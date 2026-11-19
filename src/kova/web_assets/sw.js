/* Only public shell resources belong here. Never cache authenticated API data. */
const CACHE = 'kova-shell-v12';
const SHELL = ['/app/assets/tool-cards.js', '/app/assets/tool-cards.css', '/app/assets/agent-tree.js', '/app/assets/agent-tree.css', '/app/assets/code-header.js', '/app/assets/code-header.css', '/app', '/app/assets/app.css', '/app/assets/toast.css', '/app/assets/status-bar.css', '/app/assets/app.js', '/app/assets/starters.js', '/app/assets/theme.js', '/app/assets/toast.js', '/app/assets/status-bar.js', '/app/assets/sounds.js', '/app/assets/diff.js', '/app/assets/diff.css', '/app/assets/thinking.css', '/app/assets/reasoning.css', '/app/assets/reasoning.js', '/app/assets/bolt.svg', '/app/assets/chat-appearance.css', '/app/assets/chat-appearance.js', '/app/assets/chat_list.css', '/app/assets/chat_list.js', '/app/assets/shortcuts.js', '/app/assets/typography.js', '/app/assets/motion.js', '/app/assets/composer-history.js', '/app/assets/composer-mentions.js', '/app/assets/markdown.css', '/app/assets/markdown-view.js', '/app/assets/panes.js', '/app/assets/panes.css', '/app/assets/plan-card.js', '/app/assets/plan-card.css', '/app/assets/drafts.js', '/app/assets/mobile.js', '/app/assets/mobile.css', '/app/assets/projects.js', '/app/assets/projects.css', '/app/assets/tokens.css', '/app/assets/palette.js', '/app/assets/palette.css', '/app/assets/settings.js', '/app/assets/settings.css', '/app/assets/theme-editor.js', '/app/assets/theme-editor.css', '/app/assets/message-actions.js', '/app/assets/message-actions.css', '/app/assets/diff-panel.js', '/app/assets/diff-panel.css', '/app/assets/compare.js', '/app/assets/compare.css', '/app/assets/theme-gallery.js', '/app/assets/theme-gallery.css', '/app/assets/thinking.js', '/app/assets/focus-trap.js', '/app/assets/custom_css.js',
  '/app/assets/vendor/marked.js', '/app/assets/vendor/purify.js', '/app/assets/vendor/highlight.js',
  '/app/assets/fonts/InterVariable.woff2', '/app/assets/fonts/InterVariable-Italic.woff2', '/app/assets/fonts/JetBrainsMonoVariable.woff2', '/app/assets/icon.svg', '/app/assets/mark.svg',
  '/app/assets/icon-32.png', '/app/assets/icon-180.png', '/app/assets/icon-192.png', '/app/assets/icon-512.png'];
// Share-sheet files wait here for the page, which uploads them through the
// normal authenticated attachment API. They never become cached API data.
const SHARE_CACHE = 'kova-share-v1';
const SHARE_TTL = 5 * 60 * 1000;
const SHARE_MAX_BYTES = 2 * 1024 * 1024;
const SHARE_MAX_FILES = 3;

self.addEventListener('install', event => event.waitUntil(
  caches.open(CACHE).then(cache => cache.addAll(SHELL))));
self.addEventListener('activate', event => event.waitUntil(Promise.all([
  caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('kova-shell-') && key !== CACHE).map(key => caches.delete(key)))),
  self.clients.claim()
])));

function shareProblem(status, message) {
  return new Response(message, {status, headers: {'Content-Type': 'text/plain; charset=utf-8'}});
}

function shareToken() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
}

async function pruneShares(cache) {
  for (const request of await cache.keys()) {
    const url = new URL(request.url || request);
    if (!url.pathname.startsWith('/app/share/')) continue;
    const response = await cache.match(request);
    const created = Number(response?.headers.get('X-Kova-Share-Created')) || 0;
    if (!created || Date.now() - created > SHARE_TTL) await cache.delete(request);
  }
}

async function receiveShare(request) {
  try {
    const form = await request.formData();
    const files = form.getAll('files').filter(value => value && typeof value === 'object' && 'size' in value);
    if (files.length > SHARE_MAX_FILES) return shareProblem(413, 'Share at most three files.');
    if (files.reduce((total, file) => total + Number(file.size || 0), 0) > SHARE_MAX_BYTES) {
      return shareProblem(413, 'Share is too large (2 MB maximum).');
    }
    const fields = {};
    for (const name of ['title', 'text', 'url']) {
      const value = form.get(name);
      if (typeof value === 'string') fields[name] = value;
    }
    const created = Date.now();
    const token = shareToken();
    const root = new URL(`/app/share/${token}`, self.location.origin).href;
    const cache = await caches.open(SHARE_CACHE);
    await pruneShares(cache);
    const entries = [];
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      const key = `${root}/file/${index}`;
      await cache.put(key, new Response(file, {headers: {
        'Content-Type': file.type || 'application/octet-stream',
        'X-Kova-Share-Created': String(created),
      }}));
      entries.push({name: file.name || 'file', type: file.type || 'application/octet-stream',
                    size: file.size, key});
    }
    const text = ['title', 'text', 'url'].map(name => (fields[name] || '').trim()).filter(Boolean).join('\n\n');
    await cache.put(`${root}/manifest`, new Response(JSON.stringify({created, text, files: entries}), {
      headers: {'Content-Type': 'application/json', 'X-Kova-Share-Created': String(created)},
    }));
    return Response.redirect(new URL(`/app#share=${token}`, self.location.origin).href, 303);
  } catch (_) {
    return shareProblem(400, 'This share could not be read.');
  }
}

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method === 'POST' && url.origin === self.location.origin && url.pathname === '/app/share') {
    event.respondWith(receiveShare(event.request));
    return;
  }
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.search || !SHELL.includes(url.pathname)) return;
  event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request).catch(async () => {
    // A cached shell is safe; it contains no chats. APIs still require a live server.
    const cached = await caches.match(event.request);
    if (cached) return cached;
    return new Response('<h1>Kova is offline</h1><p>Reconnect to your computer, then reopen Kova. Chats and reminders need the server.</p>', {status: 503, headers: {'Content-Type': 'text/html'}});
  })));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(self.clients.matchAll({type: 'window'}).then(async clients => {
    const page = clients.find(client => new URL(client.url).pathname === '/app');
    if (page) return page.focus();
    return self.clients.openWindow('/app');
  }));
});

// The authenticated page forwards a toast's own wording (reminders use the same
// path). No push service: the page is alive, it is just hidden.
self.addEventListener('message', event => {
  const data = event.data;
  const reminder = data?.type === 'reminder';
  if ((!reminder && data?.type !== 'toast') || typeof data.text !== 'string' || !data.text) return;
  event.waitUntil(self.clients.matchAll({type: 'window'}).then(clients => {
    if (clients.some(client => client.focused)) return;
    const title = !reminder && typeof data.title === 'string' && data.title ? data.title : 'Reminder';
    return self.registration.showNotification(title, {
      body: data.text, tag: `${reminder ? 'reminder' : 'toast'}-${data.id || data.text}`,
      icon: '/app/assets/icon-192.png'
    });
  }));
});
