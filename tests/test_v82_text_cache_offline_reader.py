import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
STYLE = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
PRIVATE_CACHE = (ROOT / "web" / "offline-cache.js").read_text(encoding="utf-8")
SERVICE_WORKER = (ROOT / "web" / "service-worker.js").read_text(encoding="utf-8")
LOCAL_WEB = (ROOT / "tools" / "local_web.py").read_text(encoding="utf-8")
READER = (ROOT / "memory" / "conversation_reader.py").read_text(encoding="utf-8")


def function_body(source: str, function_name: str) -> str:
    marker = f"function {function_name}("
    start = source.index(marker)
    open_paren = source.index("(", start)
    paren_depth = 0
    quote = ""
    escaped = False
    close_paren = -1
    for index in range(open_paren, len(source)):
        character = source[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {'"', "'", "`"}:
            quote = character
        elif character == "(":
            paren_depth += 1
        elif character == ")":
            paren_depth -= 1
            if paren_depth == 0:
                close_paren = index
                break
    if close_paren < 0:
        raise AssertionError(f"unterminated parameters for {function_name}")
    open_brace = source.index("{", close_paren)
    depth = 0
    quote = ""
    escaped = False
    for index in range(open_brace, len(source)):
        character = source[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {'"', "'", "`"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace + 1:index]
    raise AssertionError(f"unterminated function {function_name}")


class V82TextCacheOfflineReaderContractTest(unittest.TestCase):
    def test_e2_keeps_e1_schema_and_30_day_ttl(self) -> None:
        self.assertIn("DB_VERSION = 1", PRIVATE_CACHE)
        self.assertIn('CONTENT_SCHEMA_VERSION = "v8.2-e1-1"', PRIVATE_CACHE)
        self.assertIn("TTL_MS = 30 * DAY_MS", PRIVATE_CACHE)
        normalizer = function_body(PRIVATE_CACHE, "normalizeMessageWindow")
        self.assertIn("expiresAt: at + TTL_MS", normalizer)
        self.assertIn("validatedAt: at", normalizer)
        toucher = function_body(PRIVATE_CACHE, "touchMessageWindow")
        self.assertIn("lastAccessedAt = at", toucher)
        self.assertNotIn("validatedAt =", toucher)

    def test_message_and_segment_commit_with_stats_in_one_transaction(self) -> None:
        writer = function_body(PRIVATE_CACHE, "saveMessageWindowOnce")
        self.assertIn(
            '[META_STORE, "conversations", "messages", "segments"]',
            writer,
        )
        self.assertIn('meta.get("stats")', writer)
        self.assertIn("messages.put(message)", writer)
        self.assertIn("segments.put(window.segment)", writer)
        self.assertIn("meta.put(stats)", writer)
        self.assertIn("transaction.abort()", writer)
        self.assertIn("messageWindowMutationHook?.(", writer)

    def test_high_level_cache_api_is_centralized(self) -> None:
        for name in (
            "saveMessageWindow",
            "getCachedMessageContext",
            "getCachedConversationWindow",
            "getCachedConversationCatalog",
            "touchMessageWindow",
            "markActiveReaderWindow",
            "releaseActiveReaderWindow",
        ):
            self.assertIn(name, PRIVATE_CACHE)
        mobile = APP.split("function mobileVisibleCharacterCount", 1)[1].split(
            "function splitTags", 1
        )[0]
        for forbidden in ("indexedDB.open(", ".objectStore(", ".transaction("):
            self.assertNotIn(forbidden, mobile)

    def test_cached_records_are_minimal_text_only_objects(self) -> None:
        normalizer = function_body(PRIVATE_CACHE, "normalizeMessageWindow")
        for required in (
            "conversationId",
            "messageId",
            "role",
            "text",
            "messageTime",
            "contentVersion",
            "segmentKey",
            "messageIds",
            "anchorMessageId",
            "cursorBefore",
            "cursorAfter",
            "reachedStart",
            "reachedEnd",
        ):
            self.assertIn(required, normalizer)
        for forbidden in (
            "authorization",
            "cookie",
            "tailnet",
            "tailscale",
            "sqlite",
            "model_trace",
            "model_slug",
            "reference_uri",
            "blob",
            "attachment",
        ):
            self.assertNotIn(forbidden, normalizer.lower())

    def test_only_two_readonly_message_apis_trigger_text_cache(self) -> None:
        preview = function_body(APP, "loadMobileContextPreview")
        reader = function_body(APP, "loadMobileConversationSegment")
        saver = function_body(APP, "saveMobileMessageResponse")
        search = function_body(APP, "runMobileSearch")
        self.assertIn("/api/message-context?", preview)
        self.assertIn("saveMobileMessageResponse", preview)
        self.assertIn("/api/conversation-messages?", APP)
        self.assertIn("saveMobileMessageResponse", reader)
        self.assertNotIn("saveMobileMessageResponse", search)
        self.assertNotIn('putRecord("searchSnapshots"', APP)
        self.assertNotIn('putRecord("mediaObjects"', APP)
        self.assertNotIn('putRecord("thumbnails"', APP)
        self.assertIn("windowAnchorMessageId", saver)
        self.assertIn("messages.some(", saver)

    def test_fallback_is_limited_to_network_timeout_and_retryable_5xx(self) -> None:
        fallback = function_body(APP, "allowsPrivateCacheFallback")
        for status in ("502", "503", "504"):
            self.assertIn(status, fallback)
        self.assertIn("isNetworkError", fallback)
        self.assertIn("isTimeout", fallback)
        for status in ("400", "401", "403", "404", "409", "422"):
            self.assertNotIn(status, fallback)
        self.assertIn("MOBILE_MESSAGE_REQUEST_TIMEOUT_MS", APP)

    def test_partial_boundaries_and_gap_copy_are_explicit(self) -> None:
        for copy in (
            "更早消息未缓存",
            "后续消息未缓存",
            "更早内容状态未知",
            "后续内容状态未知",
            "此处还有未缓存消息",
        ):
            self.assertIn(copy, APP)
        cached_reader = function_body(PRIVATE_CACHE, "getCachedConversationWindow")
        self.assertIn("gapsBeforeMessageIds", cached_reader)
        self.assertIn("currentFirst > coverageLastPosition + 1", cached_reader)
        self.assertIn("reachedStart", cached_reader)
        self.assertIn("reachedEnd", cached_reader)

    def test_offline_copy_catalog_and_revalidation_entry_are_present(self) -> None:
        for token in (
            'id="mobile-offline-home"',
            "当前仅可使用已缓存内容",
            "搜索和阅读仅使用本机仍有效的缓存副本",
            'id="mobile-reader-offline-banner"',
            "当前显示本机缓存副本",
            "内容可能落后于主机",
            'id="mobile-reader-revalidate"',
        ):
            self.assertIn(token, INDEX)
        self.assertIn("getCachedConversationCatalog", APP)
        self.assertIn("reloadMobileConversationFromNetwork", APP)
        catalog = function_body(APP, "renderMobileOfflineCatalog")
        self.assertIn("mobileOfflineCatalogRequestSerial", catalog)
        self.assertIn("replaceChildren(", catalog)
        offline_surface = function_body(APP, "syncMobileOfflineSurface")
        self.assertIn('dataset.mobileView !== "home"', offline_surface)
        self.assertIn("#mobile-reader-offline-banner", STYLE)

    def test_api_contract_exposes_stable_position_without_extra_writes(self) -> None:
        self.assertIn('"position": int(row["position"])', READER)
        self.assertIn('item["position"] = int(row["position"])', LOCAL_WEB)
        self.assertIn("ordered.position", LOCAL_WEB)
        sender = LOCAL_WEB.split("    def send_json", 1)[1].split(
            "    def send_error_json", 1
        )[0]
        self.assertIn('self.send_header("Cache-Control", "no-store")', sender)

    def test_service_worker_stays_static_only_at_e2_version(self) -> None:
        self.assertIn('APP_SHELL_VERSION = "v10-e-agency-workspace-3"', SERVICE_WORKER)
        api_branch = SERVICE_WORKER.split(
            'if (url.pathname.startsWith("/api/"))', 1
        )[1].split('if (request.mode === "navigate")', 1)[0]
        self.assertIn("event.respondWith(fetch(request))", api_branch)
        self.assertNotIn("cache", api_branch.lower())
        precache = SERVICE_WORKER.split(
            "const APP_SHELL_URLS = Object.freeze([", 1
        )[1].split("]);", 1)[0]
        self.assertNotRegex(precache, re.compile(r"/api/", re.IGNORECASE))

    def test_mobile_e2_remains_get_only_without_sync_or_media(self) -> None:
        mobile = APP.split("function mobileVisibleCharacterCount", 1)[1].split(
            "function splitTags", 1
        )[0]
        for forbidden in (
            'method: "POST"',
            'method: "PUT"',
            'method: "PATCH"',
            'method: "DELETE"',
            "BackgroundSync",
            "PeriodicSync",
            "sync.register",
            "caches.open",
        ):
            self.assertNotIn(forbidden, mobile)
        for forbidden in ("BackgroundSync", "PeriodicSync", "sync.register"):
            self.assertNotIn(forbidden, SERVICE_WORKER)


if __name__ == "__main__":
    unittest.main()
