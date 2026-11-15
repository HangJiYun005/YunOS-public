"use strict";

const APP_SHELL_CACHE_PREFIX = "yunos-app-shell-";
const APP_SHELL_VERSION = "v10-e-agency-workspace-3";
const APP_SHELL_CACHE = `${APP_SHELL_CACHE_PREFIX}${APP_SHELL_VERSION}`;
const OFFLINE_SHELL_KEY = "/offline-shell.html";
const APP_SHELL_URLS = Object.freeze([
  "/",
  "/index.html",
  OFFLINE_SHELL_KEY,
  "/style.css?v=10-e-1",
  "/i18n/en.js?v=9-0-1-i18n-3",
  "/i18n/zh-CN.js?v=9-0-1-i18n-3",
  "/i18n/en-shell-mobile-extra.js?v=10-e-2",
  "/i18n/zh-CN-shell-mobile-extra.js?v=10-e-2",
  "/i18n/en-legacy-core.js?v=9-0-1-i18n-3",
  "/i18n/zh-CN-legacy-core.js?v=9-0-1-i18n-3",
  "/i18n/en-legacy-tools.js?v=9-0-1-i18n-3",
  "/i18n/zh-CN-legacy-tools.js?v=9-0-1-i18n-3",
  "/i18n/en-operational-extra.js?v=9-0-1-i18n-3",
  "/i18n/zh-CN-operational-extra.js?v=9-0-1-i18n-3",
  "/i18n/index.js?v=9-0-1-i18n-3",
  "/pwa.js?v=9-0-1-i18n-3",
  "/offline-cache.js?v=9-0-1-i18n-3",
  "/app.js?v=10-e-3",
  "/manifest.webmanifest",
  "/icons/yunos-192.png",
  "/icons/yunos-512.png",
  "/icons/yunos-maskable-512.png",
  "/icons/yunos-apple-touch-180.png",
]);
const APP_SHELL_REQUEST_KEYS = new Set(
  APP_SHELL_URLS.map((value) => new URL(value, self.location.origin).href),
);

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(APP_SHELL_CACHE);
    await cache.addAll(APP_SHELL_URLS);
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const cacheNames = await caches.keys();
    await Promise.all(cacheNames
      .filter((cacheName) => (
        cacheName.startsWith(APP_SHELL_CACHE_PREFIX)
        && cacheName !== APP_SHELL_CACHE
      ))
      .map((cacheName) => caches.delete(cacheName)));
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (event.data?.type === "YUNOS_ACTIVATE_UPDATE") self.skipWaiting();
});

async function networkFirstNavigation(request) {
  try {
    const response = await fetch(request);
    if (response.status < 500) return response;
  } catch {
    // Fall through to the cached application shell.
  }
  const cache = await caches.open(APP_SHELL_CACHE);
  return (await cache.match("/index.html"))
    || (await cache.match("/"))
    || (await cache.match(OFFLINE_SHELL_KEY))
    || Response.error();
}

async function cacheFirstStatic(request) {
  const cache = await caches.open(APP_SHELL_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) await cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/api/")) {
    event.respondWith(fetch(request));
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(networkFirstNavigation(request));
    return;
  }

  if (APP_SHELL_REQUEST_KEYS.has(url.href)) {
    event.respondWith(cacheFirstStatic(request));
  }
});
