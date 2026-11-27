// Service worker de Biblioteca de Prozac.
// - Miniaturas: se guardan en el teléfono (cache primero), así las portadas cargan al instante y sin internet.
// - Páginas (/): primero la red; si no hay conexión, la última copia que viste, o la página «Sin conexión».
// - Audio, descargas, lectores y la API NUNCA se tocan (siempre van directo al servidor).
const VERSION = 'v1';
const SHELL = 'shell-' + VERSION, THUMBS = 'thumbs-' + VERSION, PAGES = 'pages-' + VERSION;
const SHELL_FILES = ['/offline', '/static/icons/icon-192.png', '/static/icons/icon-512.png'];

self.addEventListener('install', (e) => {
    e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
    e.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(keys.filter((k) => !k.endsWith('-' + VERSION)).map((k) => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

async function trim(cache, max) {
    const keys = await cache.keys();
    for (let i = 0; i < keys.length - max; i++) await cache.delete(keys[i]);
}

async function cacheFirst(req, name, max) {
    const cache = await caches.open(name);
    const hit = await cache.match(req);
    if (hit) return hit;
    const res = await fetch(req);
    if (res.ok) { cache.put(req, res.clone()); trim(cache, max); }
    return res;
}

async function staleWhileRevalidate(req, name) {
    const cache = await caches.open(name);
    const hit = await cache.match(req);
    const net = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; }).catch(() => hit);
    return hit || net;
}

async function networkFirst(req, name, max) {
    const cache = await caches.open(name);
    try {
        const res = await fetch(req);
        if (res.ok && res.headers.get('content-type') && res.headers.get('content-type').includes('text/html')) {
            cache.put(req.url, res.clone()); trim(cache, max);
        }
        return res;
    } catch (err) {
        return (await cache.match(req.url)) || (await caches.match('/offline')) || Response.error();
    }
}

self.addEventListener('fetch', (e) => {
    const req = e.request;
    if (req.method !== 'GET') return;
    const url = new URL(req.url);
    if (url.origin !== self.location.origin) return;
    if (req.headers.has('range')) return;                       // audio/video/PDF por partes: directo a la red
    const p = url.pathname;
    if (p.startsWith('/thumb/')) return e.respondWith(cacheFirst(req, THUMBS, 800));
    if (p.startsWith('/static/') || p === '/offline') return e.respondWith(staleWhileRevalidate(req, SHELL));
    if (p === '/') return e.respondWith(networkFirst(req, PAGES, 30));
});
