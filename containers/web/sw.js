/* Service worker de Glaciar: precache mínimo y cache-first para media pesada. */
// v2: the infrastructure panel summary (/infra/) must never be served from a cache.
const VERSION = 'glaciar-puffle-v2';
const ESTATICO = `pre-${VERSION}`;
const MEDIA = `media-${VERSION}`;
const PRECACHE = ['/', '/register.html', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/apple-touch-icon.png'];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(ESTATICO).then(cache => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (!name.endsWith(VERSION)) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  // Solo same-origin GET; nunca interfiere con sockets, salud, registro ni navegación offline.
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/socket/') || url.pathname === '/healthz' || url.pathname.startsWith('/create/') || url.pathname.startsWith('/puffle/')) return;
  // Monitoring summary: always live from the network, never cached, never a cached fallback.
  if (url.pathname.startsWith('/infra/')) {
    event.respondWith(fetch(event.request, {cache: 'no-store'}));
    return;
  }
  // The protocol renderer must match the deployed world, including after rollback.
  if (url.pathname.endsWith('/engine.swf')) {
    event.respondWith(fetch(event.request, {cache: 'no-store'}));
    return;
  }

  // Media y runtime de Ruffle: cache-first con refresco en segundo plano (180 días de vida útil).
  if (url.pathname.startsWith('/media/') || url.pathname.startsWith('/ruffle/') || url.pathname.startsWith('/play/')) {
    event.respondWith(
      caches.open(MEDIA).then(async cache => {
        const cached = await cache.match(event.request);
        const refresco = fetch(event.request).then(response => {
          if (response.ok) cache.put(event.request, response.clone());
          return response;
        }).catch(() => cached);
        return cached || refresco;
      })
    );
    return;
  }

  // Navegación del shell: red primero, lobby cacheado como respaldo.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).then(response => {
        const copia = response.clone();
        caches.open(ESTATICO).then(cache => cache.put('/', copia));
        return response;
      }).catch(() => caches.match('/'))
    );
  }
});
