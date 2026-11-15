import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
PRIVATE_CACHE = (ROOT / "web" / "offline-cache.js").read_text(encoding="utf-8")
SERVICE_WORKER = (ROOT / "web" / "service-worker.js").read_text(encoding="utf-8")
LOCAL_WEB = (ROOT / "tools" / "local_web.py").read_text(encoding="utf-8")


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


class V82PrivateCacheCoreContractTest(unittest.TestCase):
    def test_private_cache_is_an_independent_lazy_mobile_module(self) -> None:
        self.assertIn('src="/offline-cache.js?v=9-0-1-i18n-3"', INDEX)
        self.assertLess(
            INDEX.index('src="/offline-cache.js?v=9-0-1-i18n-3"'),
            INDEX.index('src="/app.js?v=10-e-3"'),
        )
        initializer = function_body(APP, "initializeMobilePrivateCacheCore")
        self.assertIn("isMobileSearchSurfaceActive()", initializer)
        self.assertIn("await cache.open()", initializer)
        self.assertIn('dataset.yunosPrivateCacheState = "ready"', initializer)
        for forbidden in ("putRecord(", "getRecord(", "cleanup(", "repair("):
            self.assertNotIn(forbidden, initializer)

    def test_schema_names_versions_and_store_keys_are_explicit(self) -> None:
        for token in (
            'DEFAULT_DB_NAME = "yunos-private-content"',
            'RESUME_DB_NAME = "yunos-mobile-resume"',
            "DB_VERSION = 1",
            'CONTENT_SCHEMA_VERSION = "v8.2-e1-1"',
            'meta: Object.freeze({ keyPath: "key"',
            'keyPath: Object.freeze(["conversationId", "messageId"])',
            'keyPath: "segmentKey"',
            'keyPath: "snapshotKey"',
            'keyPath: "mediaKey"',
            '"by_expires_at"',
            '"by_last_accessed_at"',
        ):
            self.assertIn(token, PRIVATE_CACHE)
        for store in (
            "conversations",
            "messages",
            "segments",
            "searchSnapshots",
            "thumbnails",
            "mediaObjects",
        ):
            self.assertIn(f'"{store}"', PRIVATE_CACHE)

    def test_upgrade_is_transactional_block_aware_and_closes_old_connections(self) -> None:
        opener = function_body(PRIVATE_CACHE, "open")
        self.assertIn('request.addEventListener("upgradeneeded"', opener)
        self.assertIn("migrationHook?.(", opener)
        self.assertIn("transaction.abort()", opener)
        self.assertIn('"schema_upgrade_blocked"', opener)
        self.assertIn('database.addEventListener("versionchange"', opener)
        self.assertIn("database.close()", opener)
        self.assertIn("validateSchema(database)", PRIVATE_CACHE)
        self.assertIn('"incompatible_schema"', PRIVATE_CACHE)

    def test_ttl_capacity_and_persistence_policy_match_e1(self) -> None:
        self.assertIn("TTL_MS = 30 * DAY_MS", PRIVATE_CACHE)
        self.assertIn("record.expiresAt = Number(record.expiresAt)", PRIVATE_CACHE)
        self.assertIn("record.validatedAt + TTL_MS", PRIVATE_CACHE)
        capacity = function_body(PRIVATE_CACHE, "estimateCapacity")
        self.assertIn("storageManager.estimate()", capacity)
        self.assertIn("storageManager.persisted()", capacity)
        self.assertIn("requestPersistence && !persisted", capacity)
        self.assertIn("storageManager.persist()", capacity)
        self.assertIn("Math.min(PRODUCT_TARGET_BYTES, Math.floor(quota * 0.5))", capacity)
        self.assertIn("Math.min(PRODUCT_CEILING_BYTES, Math.floor(quota * 0.7))", capacity)
        self.assertIn("DEFAULT_TARGET_BYTES", capacity)
        self.assertIn("DEFAULT_CEILING_BYTES", capacity)
        self.assertNotIn("persist()", function_body(PRIVATE_CACHE, "open"))

    def test_stats_and_record_write_share_one_transaction(self) -> None:
        writer = function_body(PRIVATE_CACHE, "putRecordOnce")
        self.assertIn(
            'database.transaction([META_STORE, category], "readwrite")',
            writer,
        )
        self.assertIn('meta.get("stats")', writer)
        self.assertIn("store.put(record)", writer)
        self.assertIn("meta.put(stats)", writer)
        self.assertIn("transaction.abort()", writer)
        self.assertIn("logicalByteSize", PRIVATE_CACHE)
        self.assertIn("categoryBytes", PRIVATE_CACHE)
        self.assertIn("categoryCounts", PRIVATE_CACHE)

    def test_quota_retry_is_bounded_to_one_cleanup_and_one_retry(self) -> None:
        writer = function_body(PRIVATE_CACHE, "putRecord")
        self.assertIn("for (let attempt = 0; attempt < 2; attempt += 1)", writer)
        self.assertIn("if (!isQuotaExceeded(error) || attempt === 1) throw error", writer)
        self.assertEqual(writer.count("cleanupCore(token"), 2)
        self.assertIn("emergency: true", writer)
        self.assertIn("effectiveCeiling", writer)

    def test_cleanup_priority_resume_and_protection_are_explicit(self) -> None:
        phase_order = [
            'id: "expired"',
            'id: "attachments"',
            'id: "originals"',
            'id: "thumbnails"',
            'id: "searchSnapshots"',
            'id: "segments"',
            'id: "messages"',
        ]
        positions = [PRIVATE_CACHE.index(token) for token in phase_order]
        self.assertEqual(positions, sorted(positions))
        cleanup = function_body(PRIVATE_CACHE, "cleanupCore")
        for token in (
            'previous?.state === "interrupted"',
            'state: "interrupted"',
            "protectedTokens.has(tokenized)",
            "phase.orphanOnly",
            "await yieldControl()",
            "effectiveTarget * ratio",
        ):
            self.assertIn(token, cleanup)

    def test_repair_framework_covers_blob_segment_snapshot_and_stats(self) -> None:
        for token in (
            "mediaRecordIsComplete",
            "record.complete !== true",
            "record.contentLength",
            "missingMessageIds",
            "resultRefs: kept",
            "recomputeStats(token)",
            'state: "interrupted"',
        ):
            self.assertIn(token, PRIVATE_CACHE)

    def test_single_writer_lease_is_checked_in_every_mutation_family(self) -> None:
        for function_name in (
            "putRecordOnce",
            "deleteBatch",
            "recordCapacity",
            "setOperationState",
            "recomputeStats",
            "clearPrivateCache",
        ):
            self.assertIn(
                "assertAndRenewLease",
                function_body(PRIVATE_CACHE, function_name),
                function_name,
            )
        self.assertIn("LeaseBusyError", PRIVATE_CACHE)
        self.assertIn("expiresAt: at + leaseMs", PRIVATE_CACHE)
        self.assertIn("releaseLease(token)", function_body(PRIVATE_CACHE, "withLease"))

    def test_clear_removes_private_content_resume_and_legacy_state_not_shell(self) -> None:
        clearer = function_body(PRIVATE_CACHE, "clearPrivateCache")
        self.assertIn("transaction.objectStore(category).clear()", clearer)
        self.assertIn("emptyStats(at)", clearer)
        self.assertIn("deleteDatabase(indexedDb, RESUME_DB_NAME)", clearer)
        self.assertIn("LEGACY_MOBILE_HISTORY_KEY", clearer)
        self.assertIn("appShellPreserved: true", clearer)
        self.assertNotIn("caches.", clearer)

    def test_private_core_has_no_network_or_real_content_integration(self) -> None:
        for forbidden in (
            "fetch(",
            "caches.open",
            "/api/search",
            "/api/conversation",
            "sqlite",
            "tailscale",
            "authorization",
            "cookie",
        ):
            self.assertNotIn(forbidden, PRIVATE_CACHE.lower())
        self.assertNotIn("putRecord(", APP)

    def test_private_api_is_no_store_in_server_and_frontend(self) -> None:
        sender = LOCAL_WEB.split("    def send_json", 1)[1].split(
            "    def send_error_json",
            1,
        )[0]
        self.assertIn('self.send_header("Cache-Control", "no-store")', sender)
        api = function_body(APP, "api")
        self.assertIn('cache: "no-store"', api)
        self.assertIn("detail: { available: true }", api)
        self.assertIn("detail: { available: false }", api)

    def test_service_worker_keeps_api_network_only_and_full_shell_fallback(self) -> None:
        api_branch = SERVICE_WORKER.split(
            'if (url.pathname.startsWith("/api/"))',
            1,
        )[1].split('if (request.mode === "navigate")', 1)[0]
        self.assertIn("event.respondWith(fetch(request))", api_branch)
        self.assertNotIn("cache", api_branch.lower())
        navigation = function_body(SERVICE_WORKER, "networkFirstNavigation")
        self.assertIn("response.status < 500", navigation)
        self.assertLess(
            navigation.index('cache.match("/index.html")'),
            navigation.index("cache.match(OFFLINE_SHELL_KEY)"),
        )
        self.assertIn('APP_SHELL_VERSION = "v10-e-agency-workspace-3"', SERVICE_WORKER)
        self.assertIn('"/offline-cache.js?v=9-0-1-i18n-3"', SERVICE_WORKER)
        precache = SERVICE_WORKER.split(
            "const APP_SHELL_URLS = Object.freeze([",
            1,
        )[1].split("]);", 1)[0]
        self.assertNotRegex(precache, re.compile(r"/api/", re.IGNORECASE))

    def test_mobile_network_contract_remains_get_only(self) -> None:
        mobile = APP.split("function mobileVisibleCharacterCount", 1)[1].split(
            "function splitTags",
            1,
        )[0]
        for forbidden in (
            'method: "POST"',
            'method: "PUT"',
            'method: "PATCH"',
            'method: "DELETE"',
            "BackgroundSync",
        ):
            self.assertNotIn(forbidden, mobile)


if __name__ == "__main__":
    unittest.main()
