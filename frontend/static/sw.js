// AI Shuffle Service Worker
// Version: 2.0.0
// Auto-update enabled - clients reload automatically on new version

const CACHE_VERSION = 8;
const CACHE_NAME = `ai-shuffle-v${CACHE_VERSION}`;
const OFFLINE_URL = '/offline.html';

// Static assets to cache on install (minimal - let network-first handle the rest)
const STATIC_ASSETS = [
  '/offline.html',
  '/manifest.json',
  '/favicon.svg',
  '/icons/icon.svg',
  '/icons/icon-maskable.svg'
];

// Cache strategies
const CACHE_STRATEGIES = {
  // Cache first, then network for static assets
  cacheFirst: async (request, cacheName = CACHE_NAME) => {
    const cache = await caches.open(cacheName);
    const cachedResponse = await cache.match(request);
    if (cachedResponse) {
      return cachedResponse;
    }
    try {
      const networkResponse = await fetch(request);
      if (networkResponse.ok) {
        cache.put(request, networkResponse.clone());
      }
      return networkResponse;
    } catch (error) {
      return null;
    }
  }
};

// Install event - cache static assets individually (not addAll which is atomic
// and fails entirely if any single asset is blocked, e.g. by Cloudflare Access)
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => {
        console.log('[SW] Caching static assets');
        return Promise.allSettled(
          STATIC_ASSETS.map((asset) =>
            cache.add(asset).catch((err) => {
              console.warn(`[SW] Failed to cache ${asset}:`, err.message);
            })
          )
        );
      })
      .then(() => self.skipWaiting())
  );
});

// Activate event - clean up old caches and notify clients to reload
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((cacheNames) => {
        return Promise.all(
          cacheNames
            .filter((cacheName) => !cacheName.includes(`v${CACHE_VERSION}`))
            .map((cacheName) => {
              console.log('[SW] Deleting old cache:', cacheName);
              return caches.delete(cacheName);
            })
        );
      })
      .then(() => self.clients.claim())
      .then(() => {
        // Notify all clients that a new version is active - trigger reload
        return self.clients.matchAll({ type: 'window' }).then((clients) => {
          clients.forEach((client) => {
            client.postMessage({
              type: 'SW_UPDATED',
              version: CACHE_VERSION,
              message: 'New version available'
            });
          });
        });
      })
  );
});

// Fetch event - handle requests with appropriate strategy
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip non-GET requests
  if (request.method !== 'GET') {
    return;
  }

  // Skip WebSocket and EventSource requests
  if (url.protocol === 'ws:' || url.protocol === 'wss:') {
    return;
  }

  // Skip chrome-extension and other non-http(s) requests
  if (!url.protocol.startsWith('http')) {
    return;
  }

  // Handle different request types
  event.respondWith(handleRequest(request, url));
});

async function handleRequest(request, url) {
  // API requests - always network-only, never cache.
  // Session/streaming data changes constantly and stale cached responses
  // cause bugs (e.g., old messages appearing after "New Session" click).
  if (url.pathname.startsWith('/api/')) {
    try {
      return await fetch(request);
    } catch (error) {
      return new Response(
        JSON.stringify({ error: 'offline', message: 'You are offline' }),
        {
          status: 503,
          headers: { 'Content-Type': 'application/json' }
        }
      );
    }
  }

  // Static assets (JS, CSS, images) - cache first
  if (
    url.pathname.match(/\.(js|css|png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|eot)$/) ||
    url.pathname.startsWith('/_app/')
  ) {
    const response = await CACHE_STRATEGIES.cacheFirst(request);
    if (response) {
      return response;
    }
    return fetch(request);
  }

  // HTML pages - network first (always get fresh content, cache for offline only)
  if (request.headers.get('Accept')?.includes('text/html')) {
    try {
      const networkResponse = await fetch(request);
      if (networkResponse.ok) {
        // Cache for offline use
        const cache = await caches.open(CACHE_NAME);
        cache.put(request, networkResponse.clone());
      }
      return networkResponse;
    } catch (error) {
      // Network failed - try cache, then offline page
      const cache = await caches.open(CACHE_NAME);
      const cachedResponse = await cache.match(request);
      if (cachedResponse) {
        return cachedResponse;
      }
      const offlinePage = await cache.match(OFFLINE_URL);
      if (offlinePage) {
        return offlinePage;
      }
      return new Response('Offline', { status: 503 });
    }
  }

  // Default - try network, fall back to cache
  try {
    return await fetch(request);
  } catch (error) {
    const cache = await caches.open(CACHE_NAME);
    const cachedResponse = await cache.match(request);
    if (cachedResponse) {
      return cachedResponse;
    }
    return new Response('Offline', { status: 503 });
  }
}

// Listen for messages from the client
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }

  if (event.data && event.data.type === 'GET_VERSION') {
    event.ports[0]?.postMessage({ version: CACHE_VERSION });
  }

  if (event.data && event.data.type === 'CLEAR_CACHE') {
    event.waitUntil(
      caches.keys().then((cacheNames) => {
        return Promise.all(
          cacheNames.map((cacheName) => caches.delete(cacheName))
        );
      }).then(() => {
        // Notify client that cache was cleared
        event.ports[0]?.postMessage({ cleared: true });
      })
    );
  }

  if (event.data && event.data.type === 'CHECK_FOR_UPDATE') {
    // Force update check
    self.registration.update();
  }
});

// Background sync for offline messages (if supported)
self.addEventListener('sync', (event) => {
  if (event.tag === 'send-messages') {
    event.waitUntil(sendPendingMessages());
  }
});

async function sendPendingMessages() {
  // This would sync any messages queued while offline
  // Implementation depends on IndexedDB storage of pending messages
  console.log('[SW] Syncing pending messages...');
}

// Push notifications (for future use)
self.addEventListener('push', (event) => {
  if (!event.data) return;

  const data = event.data.json();
  const options = {
    body: data.body || 'New notification',
    icon: '/icons/icon.svg',
    badge: '/icons/icon.svg',
    vibrate: [200, 100, 200],
    data: data.data || {}
  };

  event.waitUntil(
    self.registration.showNotification(data.title || 'AI Shuffle', options)
  );
});

// Notification click handler
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  event.waitUntil(
    clients.matchAll({ type: 'window' }).then((clientList) => {
      // Focus existing window if available
      for (const client of clientList) {
        if (client.url === '/' && 'focus' in client) {
          return client.focus();
        }
      }
      // Open new window
      if (clients.openWindow) {
        return clients.openWindow('/');
      }
    })
  );
});
