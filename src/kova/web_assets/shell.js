/* The app shell of the chat-app look (redesign/chat-app-look):
   - the app switch at the top of the side list: Kova, Kova Code (not built
     yet, so it says so) and Kova Bots, which shows the bot contacts instead of
     the chat history;
   - Chat | Work at the top of a new chat; once a chat has started its mode is
     fixed, and the same spot offers to fork the chat into the other mode;
   - the context ring next to the model, fed by the status bar's numbers.
   app.js creates it once and calls refresh() whenever its controls change. */
(() => {
  'use strict';
  const APP_KEY = 'kova-app';
  const RING_KEY = 'kova-context-ring';
  const $ = id => document.getElementById(id);
  const read = (key, fallback) => { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } };
  const store = (key, value) => { try { localStorage.setItem(key, value); } catch { /* private mode */ } };

  function create({state, forkChat, chatSetting, safe, controls, switchApp}) {
    const appButton = $('app-switch'), appMenu = $('app-menu');
    const forkBox = $('mode-fork'), forkButton = $('mode-fork-button'), forkMenu = $('mode-fork-menu');
    const modeSwitch = $('mode-switch');
    if (!appButton || !appMenu || !forkBox || !modeSwitch) return null;

    let currentApp;
    function setApp(app) {
      currentApp = app;
      document.body.classList.toggle('app-bots', app === 'bots');
      $('app-switch-label').textContent = app === 'bots' ? 'Kova Bots' : 'Kova';
      for (const item of appMenu.querySelectorAll('[data-app]')) {
        if (item.getAttribute('role') === 'menuitemradio') item.setAttribute('aria-checked', String(item.dataset.app === app));
      }
      store(APP_KEY, app);
    }
    function appOpen(open) {
      appMenu.hidden = !open; appButton.setAttribute('aria-expanded', String(open));
      if (open) (appMenu.querySelector('[aria-checked="true"]:not(:disabled)') || appButton).focus();
    }
    appButton.addEventListener('click', () => appOpen(appMenu.hidden));
    appMenu.addEventListener('click', event => {
      const item = event.target.closest('[data-app]');
      if (!item || item.getAttribute('aria-disabled') === 'true') return;
      appOpen(false);
      appButton.focus();
      if (item.dataset.app !== currentApp) safe(() => switchApp(item.dataset.app));
    });
    appMenu.addEventListener('keydown', event => {
      if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      const items = [...appMenu.querySelectorAll('[role="menuitemradio"]:not(:disabled)')];
      if (!items.length) return;
      event.preventDefault();
      const current = items.indexOf(document.activeElement);
      const step = event.key === 'ArrowDown' ? 1 : -1;
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : (current + step + items.length) % items.length;
      items[next].focus();
    });
    setApp(read(APP_KEY, 'kova') === 'bots' ? 'bots' : 'kova');

    function forkOpen(open) {
      forkMenu.hidden = !open; forkButton.setAttribute('aria-expanded', String(open));
      if (open) $('mode-fork-go')?.focus();
    }
    forkButton.addEventListener('click', () => forkOpen(forkMenu.hidden));
    $('mode-fork-go').addEventListener('click', () => safe(async () => {
      const chat = state.chat; if (!chat) return;
      const target = chat.mode === 'chat' ? 'work' : 'chat';
      forkOpen(false);
      await forkChat(chat.id);
      if (state.chat && state.chat.id !== chat.id) {
        await chatSetting({mode: target});
        $('mode').value = target;
      }
      controls();
    }));

    document.addEventListener('click', event => {
      if (!event.target.closest('.app-switch')) appOpen(false);
      if (!event.target.closest('.mode-fork')) forkOpen(false);
    });
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      if (!appMenu.hidden) { appOpen(false); appButton.focus(); }
      if (!forkMenu.hidden) { forkOpen(false); forkButton.focus(); }
    });

    function refresh() {
      const bots = currentApp === 'bots';
      for (const item of appMenu.querySelectorAll('[data-app][role="menuitemradio"]')) {
        item.disabled = !!state.busy;
      }
      $('chat-list').setAttribute('aria-label', bots ? 'Bot conversations' : 'Chat history');
      $('search').setAttribute('aria-label', bots ? 'Search bot conversations' : 'Search conversations');
      $('bot-inbox-heading').hidden = !bots;
      $('new-workspace-bot').disabled = !!state.busy || !state.connected;
      const chat = state.chat;
      const botChat = !!($('bot-chat-header') && !$('bot-chat-header').hidden);
      const started = !!chat;
      const mode = chat?.mode === 'chat' ? 'chat' : (started ? 'work' : $('mode').value);
      $('mode-top').hidden = botChat;
      modeSwitch.hidden = started;
      forkBox.hidden = !started || !!chat?.ephemeral;
      $('mode-fork-label').textContent = mode === 'chat' ? 'Chat' : 'Work';
      const other = mode === 'chat' ? 'Work' : 'Chat';
      $('mode-fork-note').textContent = `This chat is in ${mode === 'chat' ? 'Chat' : 'Work'}. To switch, fork it.`;
      $('mode-fork-title').textContent = `Fork into ${other}`;
      $('mode-fork-desc').textContent = other === 'Chat'
        ? 'A new chat with this conversation, without tools'
        : 'A new chat with this conversation that can use tools';
      $('mode-fork-go').disabled = !!state.busy;
      const title = $('welcome-title');
      const directory = bots && !started;
      $('bot-directory').hidden = !directory;
      document.body.classList.toggle('bots-idle', directory);
      if (title && !started) title.textContent = mode === 'chat' ? 'What’s on your mind?' : 'What should we get done?';
    }

    return {refresh, setApp, app: () => currentApp};
  }

  /* The ring reads the same figures the status bar shows. */
  let last = {used: 0, windowSize: 0};
  function paintRing({used = 0, windowSize = 0} = last) {
    last = {used, windowSize};
    const ring = $('context-ring'); if (!ring) return;
    const on = read(RING_KEY, 'on') !== 'off';
    ring.hidden = !on || !(windowSize > 0);
    if (ring.hidden) return;
    const percent = Math.min(100, Math.floor(used / windowSize * 100));
    $('context-ring-fill').setAttribute('stroke-dasharray', `${percent} 100`);
    $('context-ring-text').textContent = `${percent}%`;
    ring.classList.toggle('is-high', percent >= 80);
    ring.setAttribute('aria-label', `Context used: ${percent} percent`);
    ring.title = `${used} of ${windowSize} context tokens used`;
  }
  function ringOn() { return read(RING_KEY, 'on') !== 'off'; }
  function setRing(on) { store(RING_KEY, on ? 'on' : 'off'); syncRingToggle(); paintRing(last); }
  function syncRingToggle() { $('context-ring-toggle')?.setAttribute('aria-checked', String(ringOn())); }
  document.addEventListener('DOMContentLoaded', () => {
    const toggle = $('context-ring-toggle'); if (!toggle) return;
    syncRingToggle();
    toggle.addEventListener('click', () => setRing(!ringOn()));
  });

  globalThis.KovaShell = {create, paintRing, setRing, ringOn};
})();
