/* Service worker for the Daily Briefing PWA. */

const CACHE = "briefing-v4";
// Briefing bodies live in their own cache so app-shell updates (which
// delete old shell caches) never wipe them.
const DATA_CACHE = "briefing-data";
const BRIEFING_KEYS = {
  daily: "./cached-briefing.json",
  news: "./cached-news.json",
};
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
    (async () => {
      // Carry over a briefing stored by an older version before deleting
      // its cache.
      const data = await caches.open(DATA_CACHE);
      if (!(await data.match(BRIEFING_KEYS.daily))) {
        const old = await caches.match(BRIEFING_KEYS.daily);
        if (old) await data.put(BRIEFING_KEYS.daily, old);
      }
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => k !== CACHE && k !== DATA_CACHE).map((k) => caches.delete(k))
      );
    })()
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // briefing.json is a static placeholder served by the public site; the
  // real briefings come through the encrypted push payload and live only
  // in the local cache (BRIEFING_KEYS).
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
      const key = BRIEFING_KEYS[payload.kind] || BRIEFING_KEYS.daily;

      // Stash the full briefing locally so the page can read it on open.
      // This is the only place the real briefing exists on the device.
      if (body) {
        try {
          const cache = await caches.open(DATA_CACHE);
          await cache.put(
            key,
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
        tag: payload.kind || "daily",
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
        if ("focus" in w) {
          if ("navigate" in w) w.navigate(target);
          return w.focus();
        }
      }
      return clients.openWindow(target);
    })
  );
});

