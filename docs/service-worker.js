/* Service worker for the Daily Briefing PWA. */

const CACHE = "briefing-v3";
const BRIEFING_KEY = "./cached-briefing.json";
const SHELL = [
  "./",
  "./index.html",
  "./styles.css",
  "./app.js",
  "./manifest.json",
  "./icon-192.png",
  "./icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // briefing.json is a static placeholder served by the public site; the
  // real briefing comes through the encrypted push payload and lives only
  // in the local cache (BRIEFING_KEY).
  event.respondWith(
    caches.match(event.request).then((hit) => hit || fetch(event.request))
  );
});

self.addEventListener("push", (event) => {
  event.waitUntil(
    (async () => {
      let payload = {};
      try { payload = event.data ? event.data.json() : {}; } catch (_) { payload = {}; }

      const headline = payload.headline || "Today's briefing is ready.";
      const body = payload.body || "";
      const generatedAt = payload.generated_at || new Date().toISOString();

      // Stash the full briefing locally so the page can read it on open.
      // This is the only place the real briefing exists on the device.
      if (body) {
        try {
          const cache = await caches.open(CACHE);
          await cache.put(
            BRIEFING_KEY,
            new Response(
              JSON.stringify({ generated_at: generatedAt, headline, body }),
              { headers: { "Content-Type": "application/json" } }
            )
          );
        } catch (e) {
          // Caching failure shouldn't block the notification.
        }
      }

      await self.registration.showNotification(payload.title || "Daily Briefing", {
        body: headline,
        icon: "icon-192.png",
        badge: "icon-192.png",
        data: { url: payload.url || "./" },
      });
    })()
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = event.notification.data?.url || "./";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ("focus" in w) return w.focus();
      }
      return clients.openWindow(target);
    })
  );
});

