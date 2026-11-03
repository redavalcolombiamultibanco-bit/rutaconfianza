/* Service worker de Ruta Confianza.
   Guarda en cache unicamente los archivos estaticos para que la app abra
   rapido desde el celular. Las rutas dinamicas y el canal en vivo de
   Socket.IO siempre van directo a la red para no mostrar datos viejos. */
const CACHE_NAME = "ruta-confianza-v1";
const PRECACHE_URLS = [
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/images/icon-192.png",
  "/static/images/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .catch(() => undefined)
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }
  // El canal en vivo y los datos del servidor nunca se sirven desde cache.
  if (url.pathname.startsWith("/socket.io/") || url.pathname.startsWith("/api/")) {
    return;
  }
  if (!url.pathname.startsWith("/static/")) {
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      const fromNetwork = fetch(request)
        .then((response) => {
          if (response && response.status === 200 && response.type === "basic") {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => cached);
      return cached || fromNetwork;
    })
  );
});
