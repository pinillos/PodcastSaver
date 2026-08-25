// Cachea solo el armazón. Los resultados NUNCA se cachean: son contenido con
// derechos de autor y el §12 pide no distribuirlo (y además caducan).
const CACHE = "podcast-kb-v1";
const ARMAZON = ["./", "index.html", "app.css", "app.js", "auth.js", "icon.svg", "manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ARMAZON)));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))),
  );
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(caches.match(e.request).then((hit) => hit ?? fetch(e.request)));
});
