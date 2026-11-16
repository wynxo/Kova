/* Optional sound cues for a finished reply, something that needs you, and a
   reminder. Off by default. Every cue is synthesized with WebAudio when it
   plays, so the app packages no audio files and fetches nothing. The choice is
   per browser/device, like notifications, and lives in localStorage. */
(() => {
  'use strict';
  const KEY = 'kova-sounds';
  const EVENTS = ['done', 'needs', 'reminder'];
  const DEFAULTS = {enabled: false, volume: 0.5, done: true, needs: true, reminder: true};
  // [frequency in Hz, delay in seconds, length in seconds]. Short and quiet.
  const CUES = {
    done: [[659.25, 0, 0.09], [880, 0.1, 0.13]],
    needs: [[523.25, 0, 0.08], [659.25, 0.1, 0.08], [783.99, 0.2, 0.1]],
    reminder: [[783.99, 0, 0.09], [622.25, 0.11, 0.14]],
  };
  const PEAK = 0.16;  // the cue's own ceiling; the volume setting scales it down

  function normalise(raw) {
    const value = {...DEFAULTS};
    if (!raw || typeof raw !== 'object') return value;
    if (typeof raw.enabled === 'boolean') value.enabled = raw.enabled;
    if (typeof raw.volume === 'number' && Number.isFinite(raw.volume)) {
      value.volume = Math.min(1, Math.max(0, raw.volume));
    }
    for (const name of EVENTS) if (typeof raw[name] === 'boolean') value[name] = raw[name];
    return value;
  }
  function read() {
    let stored = null;
    try { stored = JSON.parse(localStorage.getItem(KEY)); } catch (_) { stored = null; }
    return normalise(stored);
  }
  function save(changes) {
    const value = normalise({...read(), ...changes});
    try { localStorage.setItem(KEY, JSON.stringify(value)); } catch (_) {}
    return value;
  }

  let context = null;
  function audio() {
    if (context) return context;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    try { context = new Ctor(); } catch (_) { context = null; }
    return context;
  }
  // A sine tone through a gain envelope, so it starts and ends without a click.
  function tone(ctx, [frequency, delay, length], level) {
    const at = ctx.currentTime + delay;
    const oscillator = ctx.createOscillator(), gain = ctx.createGain();
    oscillator.type = 'sine';
    oscillator.frequency.setValueAtTime(frequency, at);
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(level, at + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + length);
    oscillator.connect(gain).connect(ctx.destination);
    oscillator.start(at);
    oscillator.stop(at + length + 0.02);
  }

  function play(name) {
    if (!EVENTS.includes(name)) return false;
    const settings = read();
    if (!settings.enabled || !settings[name] || settings.volume <= 0) return false;
    // A finished reply is only worth a cue when you are not looking at the window.
    if (name === 'done' && document.hasFocus?.()) return false;
    const ctx = audio();
    if (!ctx) return false;
    if (ctx.state === 'suspended') {
      ctx.resume?.()?.catch?.(() => {});
      // Before any click or key press a browser keeps the context suspended; say so instead of pretending.
      if (typeof navigator !== 'undefined' && navigator.userActivation && !navigator.userActivation.hasBeenActive) return false;
    }
    try {
      for (const cue of CUES[name]) tone(ctx, cue, PEAK * settings.volume);
    } catch (_) { return false; }
    return true;
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function check(text) {
    const label = el('label', 'provider-check'), input = el('input');
    input.type = 'checkbox';
    label.append(input, el('span', '', text));
    return label;
  }
  // The controls theme themselves through the shared dialog classes: no colours
  // and no layout of their own, so a theme restyle carries them along.
  function panel() {
    const dialog = el('dialog', 'panel');
    dialog.setAttribute('aria-label', 'Sounds');
    const head = el('div', 'dialog-head');
    const close = el('button', 'btn quiet', 'Close');
    close.type = 'button';
    close.onclick = () => dialog.close();
    head.append(el('h2', '', 'Sounds'), close);
    const intro = el('p', 'panel-intro', 'Short, quiet cues for a finished reply, a question or approval, and reminders. This browser makes them itself: no audio files, nothing downloaded or sent.');
    const on = check('Play sounds');
    const volume = el('label', '', 'Volume');
    const range = el('input');
    range.type = 'range'; range.min = '0'; range.max = '100'; range.step = '5';
    const level = el('output');
    volume.append(range, level);
    const events = el('div');
    const toggles = {};
    for (const [name, text] of [['done', 'Reply finished (only when this window is not focused)'],
                                ['needs', 'A question or approval needs you'],
                                ['reminder', 'A reminder']]) {
      toggles[name] = check(text); events.append(toggles[name]);
    }
    const note = el('p', 'provider-footnote', 'Sound is on or off for this browser and device, like notifications.');
    function render() {
      const settings = read();
      on.children[0].checked = settings.enabled;
      for (const name of EVENTS) {
        toggles[name].children[0].checked = settings[name];
        toggles[name].children[0].disabled = !settings.enabled;
      }
      range.value = String(Math.round(settings.volume * 100));
      range.disabled = !settings.enabled;
      level.textContent = `${Math.round(settings.volume * 100)}%`;
    }
    on.children[0].onchange = () => {
      save({enabled: on.children[0].checked});
      // Turning sounds on is a user gesture: create and resume the context now, so a later cue can play.
      if (on.children[0].checked) audio()?.resume?.()?.catch?.(() => {});
      render();
    };
    range.oninput = () => { save({volume: Number(range.value) / 100}); render(); };
    for (const name of EVENTS) {
      toggles[name].children[0].onchange = () => { save({[name]: toggles[name].children[0].checked}); render(); };
    }
    dialog.append(head, intro, on, volume, events, note);
    document.body.append(dialog);
    render();
    return dialog;
  }

  window.KovaSounds = {play, read, save, panel, EVENTS, CUES, DEFAULTS};
  const open = document.getElementById('open-sounds');
  if (open) { const dialog = panel(); open.onclick = () => dialog.showModal(); }
})();
