/* Service worker de Ruta Confianza.
   Guarda en cache unicamente los archivos estaticos para que la app abra
   rapido desde el celular. Las rutas dinamicas y el canal en vivo de
   Socket.IO siempre van directo a la red para no mostrar datos viejos. */
// Nombre versionado del cache: al cambiarlo, el evento activate borra el cache
// anterior y fuerza a descargar de nuevo la version mas reciente.
const CACHE_NAME = "ruta-confianza-v2";
// Archivos minimos para que la app pueda abrir sin internet.
const PRECACHE_URLS = [
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/images/icon-192.png",
  "/static/images/icon-512.png",
];

// INSTALL: guarda en cache los archivos base y activa el nuevo service worker
// sin esperar a que se cierren las pestanas abiertas (skipWaiting).
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .catch(() => undefined)
      .then(() => self.skipWaiting())
  );
});

// ACTIVATE: borra los caches de versiones anteriores y toma el control de las
// pestanas ya abiertas (clients.claim).
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

// FETCH: intercepta las peticiones de la app. Solo se cachean los GET del mismo
// origen que empiezan por /static/; el resto (socket.io, api y otros origenes)
// va directo a la red para no servir datos desactualizados.
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
    // Primero la red: asi las actualizaciones de la app llegan de inmediato al
    // celular. Si no hay internet, se responde con la copia guardada en cache.
    fetch(request)
      .then((response) => {
        if (response && response.status === 200 && response.type === "basic") {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request))
  );
});
