/* PGG Field App - Service Worker
 * Strategy: network-first (always fetch the latest when online, fall back to
 * cache when offline). No manual version bumping needed: updating files on
 * the server propagates automatically on the next launch that has signal.
 */
const CACHE = 'pgg-field-cache';
const PREFIX = 'pgg-field-';            // only this app's caches are cleaned up
const NET_TIMEOUT = 4000;               // ms before falling back to cache on slow networks
const SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png'
];

// Pre-cache the app shell so the app still opens with no signal.
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

// Take control immediately and remove only THIS app's stale caches
// (so it doesn't delete the moderator / survey caches on the same origin).
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k.startsWith(PREFIX) && k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// Network-first for same-origin GET requests, with a timeout fallback to cache.
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) return;

  event.respondWith((async () => {
    // Always try the network. On success, refresh the cached copy for offline use.
    const network = fetch(req).then((res) => {
      if (res && res.ok && res.type === 'basic') {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
      }
      return res;
    });
    network.catch(() => {}); // avoid an unhandled rejection if the timeout wins

    try {
      const timeout = new Promise((_, reject) =>
        setTimeout(() => reject(new Error('network-timeout')), NET_TIMEOUT)
      );
      return await Promise.race([network, timeout]);
    } catch (err) {
      // Offline or too slow -> serve whatever we have cached.
      const cached = await caches.match(req);
      if (cached) return cached;
      if (req.mode === 'navigate') {
        return (await caches.match('./')) || (await caches.match('./index.html'));
      }
      throw err;
    }
  })());
});
