import json
import re
import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
INDEX = WEB / "index.html"
STYLE = WEB / "style.css"
PWA = WEB / "pwa.js"
SERVICE_WORKER = WEB / "service-worker.js"
MANIFEST = WEB / "manifest.webmanifest"


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        signature = handle.read(24)
    if signature[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"Not a PNG: {path}")
    return struct.unpack(">II", signature[16:24])


class V82PwaFoundationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.css = STYLE.read_text(encoding="utf-8")
        cls.pwa = PWA.read_text(encoding="utf-8")
        cls.service_worker = SERVICE_WORKER.read_text(encoding="utf-8")
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_manifest_is_installable_and_complete(self) -> None:
        expected = {
            "name": "YunOS",
            "short_name": "YunOS",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait-primary",
            "theme_color": "#f7f8fa",
            "background_color": "#f7f8fa",
        }
        for key, value in expected.items():
            self.assertEqual(value, self.manifest.get(key), key)
        purposes = {icon["purpose"] for icon in self.manifest["icons"]}
        self.assertEqual({"any", "maskable"}, purposes)

    def test_manifest_and_apple_icon_paths_are_valid_pngs(self) -> None:
        icon_specs = [(icon["src"], icon["sizes"]) for icon in self.manifest["icons"]]
        icon_specs.append(("/icons/yunos-apple-touch-180.png", "180x180"))
        for src, sizes in icon_specs:
            path = WEB / src.lstrip("/")
            self.assertTrue(path.is_file(), src)
            expected = tuple(int(value) for value in sizes.split("x"))
            self.assertEqual(expected, png_size(path), src)

    def test_ios_metadata_and_safe_area_foundation_are_present(self) -> None:
        for token in (
            "viewport-fit=cover",
            'name="apple-mobile-web-app-capable" content="yes"',
            'name="apple-mobile-web-app-status-bar-style" content="default"',
            'rel="apple-touch-icon" sizes="180x180"',
            'rel="manifest" href="/manifest.webmanifest"',
        ):
            self.assertIn(token, self.html)
        for edge in ("top", "right", "bottom", "left"):
            self.assertIn(f"env(safe-area-inset-{edge}, 0px)", self.css)

    def test_service_worker_registration_is_secure_context_safe(self) -> None:
        self.assertIn('"serviceWorker" in navigator', self.pwa)
        self.assertIn("window.isSecureContext", self.pwa)
        self.assertIn('navigator.serviceWorker.register("/service-worker.js"', self.pwa)
        self.assertIn('updateViaCache: "none"', self.pwa)
        self.assertIn('scope: "/"', self.pwa)

    def test_updates_wait_for_explicit_safe_activation(self) -> None:
        install_handler = self.service_worker.split(
            'self.addEventListener("install"', 1
        )[1].split('self.addEventListener("activate"', 1)[0]
        self.assertNotIn("skipWaiting", install_handler)
        self.assertIn('type: "YUNOS_ACTIVATE_UPDATE"', self.pwa)
        self.assertIn('event.data?.type === "YUNOS_ACTIVATE_UPDATE"', self.service_worker)
        controller_change = self.pwa.split(
            'navigator.serviceWorker.addEventListener("controllerchange"', 1
        )[1].split("navigator.serviceWorker.register", 1)[0]
        self.assertIn("if (!reloadAfterActivation) return", controller_change)

    def test_cache_scope_is_static_allowlist_and_api_is_network_only(self) -> None:
        self.assertIn('const APP_SHELL_CACHE_PREFIX = "yunos-app-shell-"', self.service_worker)
        self.assertIn('url.pathname.startsWith("/api/")', self.service_worker)
        api_branch = self.service_worker.split(
            'if (url.pathname.startsWith("/api/"))', 1
        )[1].split('if (request.mode === "navigate")', 1)[0]
        self.assertIn("event.respondWith(fetch(request))", api_branch)
        self.assertNotIn("cache", api_branch.lower())
        deletion_filter = self.service_worker.split(".filter((cacheName)", 1)[1].split(").map", 1)[0]
        self.assertIn("cacheName.startsWith(APP_SHELL_CACHE_PREFIX)", deletion_filter)
        for forbidden in ("search result", "conversation", "attachment", "runtime"):
            self.assertNotIn(forbidden, self.service_worker.lower())

    def test_offline_navigation_falls_back_to_precached_shell_only(self) -> None:
        self.assertIn("networkFirstNavigation", self.service_worker)
        self.assertIn("cache.match(OFFLINE_SHELL_KEY)", self.service_worker)
        self.assertIn('const OFFLINE_SHELL_KEY = "/offline-shell.html"', self.service_worker)
        self.assertTrue((WEB / "offline-shell.html").is_file())
        self.assertNotIn("cache.put(\"/index.html\"", self.service_worker)
        self.assertIn("无法连接 YunOS 主机", self.html)
        self.assertIn("可继续阅读有效缓存副本", self.html)
        self.assertIn('document.body.dataset.yunosOfflineShell === "true"', self.pwa)

    def test_offline_host_status_collapses_after_three_seconds(self) -> None:
        for token in (
            "HOST_STATUS_EXPANDED_MS = 3000",
            "HOST_STATUS_FADE_MS = 220",
            'hostStatus.dataset.displayMode = "expanded"',
            'hostStatus.dataset.displayMode = "compact"',
            'hostStatus.classList.add("is-dismissing")',
            'hostStatus.classList.add("is-compact")',
        ):
            self.assertIn(token, self.pwa)
        self.assertIn(".pwa-host-status.is-dismissing", self.css)
        compact = self.css.split(".pwa-host-status.is-compact", 1)[1].split("}", 1)[0]
        self.assertIn("width: 8px", compact)
        self.assertIn("height: 8px", compact)
        self.assertIn("background: #dc2626", compact)
        self.assertIn("var(--yunos-safe-area-top)", compact)
        self.assertIn("var(--yunos-safe-area-right)", compact)

    def test_all_precache_urls_resolve_to_local_files(self) -> None:
        array_source = self.service_worker.split(
            "const APP_SHELL_URLS = Object.freeze([", 1
        )[1].split("]);", 1)[0]
        urls = re.findall(r'"([^"]+)"', array_source)
        self.assertGreaterEqual(len(urls), 9)
        for url in urls:
            path_only = url.split("?", 1)[0]
            if path_only == "/":
                path_only = "/index.html"
            self.assertTrue((WEB / path_only.lstrip("/")).is_file(), url)


if __name__ == "__main__":
    unittest.main()
