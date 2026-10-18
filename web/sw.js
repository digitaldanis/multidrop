// Service worker of the MultiDrop web edition: makes the page installable and
// opens it without a network. Network first, so a new version is used as soon
// as it is online; the cache is only the fallback. Relays and WebRTC are not
// HTTP and never pass through here.
const CACHE = "multidrop-web-v1";
const SHELL = ["./", "index.html", "app.js", "wire.js", "style.css", "manifest.webmanifest", "icons/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // The page's own files and the pinned libraries from esm.sh; nothing else.
  if (url.origin !== location.origin && url.hostname !== "esm.sh") return;
  event.respondWith((async () => {
    try {
      const res = await fetch(req);
      if (res.ok) (await caches.open(CACHE)).put(req, res.clone());
      return res;
    } catch (e) {
      const hit = await caches.match(req, { ignoreSearch: req.mode === "navigate" });
      if (hit) return hit;
      throw e;
    }
  })());
});
