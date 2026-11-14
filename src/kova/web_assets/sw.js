/* Only public shell resources belong here. Never cache authenticated API data. */
const CACHE = 'kova-shell-v1';
const SHELL = ['/app', '/app/assets/app.css', '/app/assets/app.js',
  '/app/assets/vendor/marked.js', '/app/assets/vendor/purify.js', '/app/assets/vendor/highlight.js',
  '/app/assets/fonts/InterVariable.woff2', '/app/assets/fonts/InterVariable-Italic.woff2', '/app/assets/fonts/JetBrainsMonoVariable.woff2', '/app/assets/icon.svg',
  '/app/assets/icon-32.png', '/app/assets/icon-180.png', '/app/assets/icon-192.png', '/app/assets/icon-512.png'];
self.addEventListener('install', event => event.waitUntil(
  caches.open(CACHE).then(cache => cache.addAll(SHELL))));
self.addEventListener('activate', event => event.waitUntil(Promise.all([
  caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('kova-shell-') && key !== CACHE).map(key => caches.delete(key)))),
  self.clients.claim()
])));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
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
