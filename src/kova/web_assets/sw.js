/* Only public shell resources belong here. Never cache authenticated API data. */
const CACHE = 'kova-shell-v1';
const SHELL = ['/app', '/app/assets/app.css', '/app/assets/app.js',
  '/app/assets/vendor/marked.js', '/app/assets/vendor/purify.js', '/app/assets/vendor/highlight.js',
  '/app/assets/fonts/InterVariable.woff2', '/app/assets/fonts/InterVariable-Italic.woff2', '/app/assets/fonts/JetBrainsMonoVariable.woff2', '/app/assets/icon.svg',
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

// The authenticated page forwards the existing /events reminder shape. No push service.
self.addEventListener('message', event => {
  if (event.data?.type !== 'reminder' || typeof event.data.text !== 'string') return;
  event.waitUntil(self.clients.matchAll({type: 'window'}).then(clients => {
    if (clients.some(client => client.focused)) return;
    return self.registration.showNotification('Reminder', {
      body: event.data.text, tag: `reminder-${event.data.id || event.data.text}`,
      icon: '/app/assets/icon-192.png'
    });
  }));
});
