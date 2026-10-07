// FaxDesk Road service worker: shows a content-free notification when the relay pokes this phone; caches the shell.
const C = "ffd-road-v1";
self.addEventListener("install", e => { self.skipWaiting(); e.waitUntil(caches.open(C).then(c => c.addAll(["/road.html"]))); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.pathname.indexOf("/road") !== 0 || u.pathname.startsWith("/road/")) return;   // relay calls go straight through
  e.respondWith(fetch(e.request).then(r => { const cp = r.clone(); caches.open(C).then(c => c.put(e.request, cp)); return r; }).catch(() => caches.match(e.request)));
});
self.addEventListener("push", e => {
  e.waitUntil(self.registration.showNotification("FaxDesk", { body: "Something new from the office. Open to see it.", icon: "/apple-touch-icon.png", badge: "/apple-touch-icon.png", tag: "ffd-road", renotify: true, data: { url: "/road.html" } }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(cs => { for (const c of cs) if (c.url.includes("/road") && "focus" in c) return c.focus(); return self.clients.openWindow("/road.html"); }));
});
