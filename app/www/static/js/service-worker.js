// Service Worker - PWA离线支持和资源缓存

const CACHE_VERSION = 'xiaosongshu-v1';
const STATIC_CACHE = `${CACHE_VERSION}-static`;
const IMAGE_CACHE = `${CACHE_VERSION}-images`;

const EXCLUDE_CACHE_PATHS = [
  '/api/',
  '/upload/',
  '.mp3', '.flac', '.wav', '.ogg', '.m4a'
];

const CRITICAL_ASSETS = [
  '/',
  '/static/css/style.css',
  '/static/css/font-awesome/all.min.css',
  '/static/js/main.js',
  '/static/js/state.js',
  '/static/js/player.js',
  '/static/js/ui.js',
  '/static/js/utils.js',
  '/static/js/api.js',
  '/static/js/db.js',
  '/static/js/netease.js',
  '/static/js/qqmusic.js',
  '/static/js/mounts.js',
  '/static/js/admin.js',
  '/static/js/lib/color-thief.umd.js',
  '/static/images/ICON_256.PNG'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => {
      const cachePromises = CRITICAL_ASSETS.map(url =>
        fetch(url, { credentials: 'same-origin' })
          .then((response) => {
            if (response && response.status === 200) {
              return cache.put(url, response).then(() => ({ status: 'fulfilled' }));
            }
            return { status: 'rejected' };
          })
          .catch(() => ({ status: 'rejected' }))
      );

      return Promise.all(cachePromises).then((results) => {
        const succeeded = results.filter(r => r.status === 'fulfilled').length;
        console.log(`[SW] 缓存完成: ${succeeded}/${CRITICAL_ASSETS.length}`);
        self.skipWaiting();
      });
    }).catch(() => { self.skipWaiting(); })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (!cacheName.startsWith('xiaosongshu-')) return Promise.resolve();
          if (cacheName === STATIC_CACHE || cacheName === IMAGE_CACHE) return Promise.resolve();
          return caches.delete(cacheName);
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  if (request.method !== 'GET') return;
  if (EXCLUDE_CACHE_PATHS.some(path => url.pathname.includes(path))) return;

  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(request)
        .then(response => response)
        .catch(() => new Response(JSON.stringify({
          success: false,
          message: '离线状态，请检查网络连接',
          offline: true
        }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' }
        }))
    );
    return;
  }

  if (request.headers.get('accept')?.includes('text/html')) {
    event.respondWith(networkFirst(request));
    return;
  }

  if (request.destination === 'style' || request.destination === 'script') {
    event.respondWith(cacheFirst(request, STATIC_CACHE));
    return;
  }

  if (url.pathname.includes('/api/music/covers/')) {
    event.respondWith(cacheFirst(request, IMAGE_CACHE));
    return;
  }

  if (request.destination === 'image') {
    event.respondWith(cacheFirst(request, IMAGE_CACHE));
    return;
  }

  event.respondWith(networkFirst(request));
});

function networkFirst(request) {
  return fetch(request)
    .then((response) => {
      if (!response || response.status !== 200 || response.type === 'error') {
        throw new Error(`HTTP ${response?.status}`);
      }
      const responseToCache = response.clone();
      caches.open(STATIC_CACHE).then((cache) => { cache.put(request, responseToCache); });
      return response;
    })
    .catch(() => {
      return caches.match(request).then((cachedResponse) => {
        if (cachedResponse) return cachedResponse;
        if (request.destination === 'document') return caches.match('/');
        return new Response('离线：资源不可用', { status: 503, statusText: 'Service Unavailable' });
      });
    });
}

function cacheFirst(request, cacheName) {
  return caches.match(request).then((cachedResponse) => {
    if (cachedResponse) return cachedResponse;
    return fetch(request)
      .then((response) => {
        if (!response || response.status !== 200) return response;
        const responseToCache = response.clone();
        caches.open(cacheName).then((cache) => { cache.put(request, responseToCache); });
        return response;
      })
      .catch(() => {
        return new Response('离线：资源不可用', { status: 503, statusText: 'Service Unavailable' });
      });
  });
}

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
