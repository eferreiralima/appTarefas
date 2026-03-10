const CACHE_NAME = "tarefas-static-v3";
const STATIC_ASSETS = [
  "/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

// instala e ativa rapidamente
self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null)));
      await self.clients.claim();
    })()
  );
});

// fetch: Network-first para navegação (HTML) e cache-first para estáticos
self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Só trata requests do seu próprio domínio
  if (url.origin !== self.location.origin) return;

  // Navegação (páginas HTML): SEMPRE tenta rede primeiro
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("/")) // fallback simples, se quiser
    );
    return;
  }

  // Só cacheia GET de arquivos estáticos
  if (req.method === "GET" && url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          return res;
        });
      })
    );
  }
});