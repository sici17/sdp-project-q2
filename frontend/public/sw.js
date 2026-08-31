const CACHE_NAME = 'arol-q2-fleet-assist-v3'
const APP_SHELL = [
  '/',
  '/index.html',
  '/offline.html',
  '/manifest.webmanifest',
  '/favicon.svg',
  '/pwa-icon.svg',
  '/pwa-maskable-icon.svg',
]

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))),
      )
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const request = event.request
  const url = new URL(request.url)

  if (request.method !== 'GET' || url.origin !== self.location.origin) {
    return
  }

  if (request.mode === 'navigate') {
    event.respondWith(navigationResponse(request))
    return
  }

  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/manuals/') ||
    url.pathname === '/metrics'
  ) {
    return
  }

  if (!APP_SHELL.includes(url.pathname) && !url.pathname.startsWith('/assets/')) {
    return
  }

  event.respondWith(cacheFirst(request))
})

async function navigationResponse(request) {
  try {
    return await fetch(request)
  } catch {
    return (await caches.match('/index.html')) ?? (await caches.match('/offline.html'))
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request)
  if (cached) {
    return cached
  }

  const response = await fetch(request)
  const cacheControl = response.headers.get('Cache-Control') ?? ''
  if (
    response.ok &&
    !/\b(?:no-store|private)\b/i.test(cacheControl) &&
    response.type !== 'opaque'
  ) {
    const cache = await caches.open(CACHE_NAME)
    await cache.put(request, response.clone())
  }
  return response
}
