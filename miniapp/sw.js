const CACHE_NAME = "reminder-pwa-v13";
const URLS = [
  "/",
  "/web",
  "/offline",
  "/manifest.webmanifest",
  "/app/static/style.css?v=19",
  "/app/static/app.js?v=23",
  "/app/static/icon.png?v=2",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(URLS)).catch(() => {}),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)),
      ),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) return; // API всегда из сети
  const isNavigation = event.request.mode === "navigate";

  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy)).catch(() => {});
        return resp;
      })
      .catch(() =>
        caches.match(event.request).then((cached) => {
          if (cached) return cached;
          if (isNavigation) return caches.match("/offline");
          return new Response("", { status: 503, statusText: "Offline" });
        }),
      ),
  );
});

