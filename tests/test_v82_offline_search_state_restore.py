import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
STYLE = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
PRIVATE_CACHE = (ROOT / "web" / "offline-cache.js").read_text(encoding="utf-8")
SERVICE_WORKER = (ROOT / "web" / "service-worker.js").read_text(encoding="utf-8")


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


class V82OfflineSearchStateRestoreContractTest(unittest.TestCase):
    def test_e3_keeps_main_schema_and_creates_only_reserved_resume_store(self) -> None:
        self.assertIn("DB_VERSION = 1", PRIVATE_CACHE)
        self.assertIn('CONTENT_SCHEMA_VERSION = "v8.2-e1-1"', PRIVATE_CACHE)
        self.assertIn('RESUME_DB_NAME = "yunos-mobile-resume"', PRIVATE_CACHE)
        self.assertIn("RESUME_DB_VERSION = 1", PRIVATE_CACHE)
        self.assertIn('RESUME_STORE = "resumeStates"', PRIVATE_CACHE)
        opener = function_body(PRIVATE_CACHE, "openResumeDatabase")
        self.assertIn("database.createObjectStore(RESUME_STORE", opener)
        self.assertIn("transaction.abort()", opener)
        self.assertIn("resumeMigrationHook?.(", opener)
        self.assertNotIn("DB_VERSION = 2", PRIVATE_CACHE)

    def test_offline_search_scans_only_complete_private_cache_records(self) -> None:
        search = function_body(PRIVATE_CACHE, "searchCachedMessages")
        sources = function_body(PRIVATE_CACHE, "cachedSearchSources")
        batch_reader = function_body(PRIVATE_CACHE, "readMessageSearchBatch")
        for token in (
            'transaction("messages", "readonly")',
            "readMessageSearchBatch",
            "segmentsByMessage",
            "completeSegmentKeys",
            "message.contentVersion === 1",
            "Number(message.expiresAt) > now()",
            "segment.partial === true",
            "missingMessageIds",
            "seenMessageIds.size === state.expectedMessageIds.size",
        ):
            self.assertIn(token, search + sources + batch_reader)
        for forbidden in (
            "fetch(",
            "/api/search",
            "sqlite",
            "caches.",
            "mediaobjects",
            "thumbnails",
        ):
            self.assertNotIn(forbidden, search.lower())

    def test_offline_search_normalization_sorting_paging_and_cancel_are_explicit(self) -> None:
        normalizer = function_body(PRIVATE_CACHE, "normalizedSearchText")
        search = function_body(PRIVATE_CACHE, "searchCachedMessages")
        self.assertIn('.normalize("NFKC")', normalizer)
        self.assertIn('.replace(/\\s+/gu, " ")', normalizer)
        self.assertIn(".toLocaleLowerCase()", normalizer)
        self.assertIn("signal?.aborted", search)
        self.assertIn("abortError(scope)", search)
        self.assertIn("await yieldControl()", search)
        self.assertIn("safeOffset", search)
        self.assertIn("safeLimit", search)
        self.assertIn("relevanceComparator", search)
        self.assertIn("latestComparator", search)
        self.assertIn("parsedMessageTime", function_body(PRIVATE_CACHE, "latestComparator"))

    def test_snapshot_is_versioned_cached_only_reference_data(self) -> None:
        saver = function_body(PRIVATE_CACHE, "saveSearchSnapshot")
        for token in (
            "snapshotVersion: SEARCH_SNAPSHOT_VERSION",
            'sourceMode: "cached-only"',
            "normalizedQuery",
            "sortMode: normalizedSort",
            "resultRefs: normalizedRefs",
            "cacheGeneration",
            "expiresAt: at + RESUME_TTL_MS",
        ):
            self.assertIn(token, saver)
        for forbidden in ("snippet", "text:", "response", "payload"):
            self.assertNotIn(forbidden, saver.lower())

    def test_resume_state_is_minimal_versioned_and_has_24_hour_gate(self) -> None:
        sanitizer = function_body(PRIVATE_CACHE, "sanitizeResumeState")
        loader = function_body(PRIVATE_CACHE, "loadResumeState")
        for token in (
            "stateVersion: RESUME_STATE_VERSION",
            "savedAt",
            "lastInteractionAt",
            "loadedResultRefs",
            "resultAnchorRef",
            "readerScrollAnchorMessageId",
            "readerScrollOffset",
            "topBarHidden",
            "cacheGeneration",
        ):
            self.assertIn(token, sanitizer)
        self.assertIn("at - savedAt > RESUME_TTL_MS", loader)
        self.assertIn("at + CLOCK_ROLLBACK_TOLERANCE_MS < savedAt", loader)
        self.assertIn("migration?.state !== \"idle\"", loader)
        self.assertIn("state.cacheGeneration !== generation", loader)
        for forbidden in (
            "authorization",
            "cookie",
            "tailnet",
            "tailscale",
            "sqlite",
            "blob",
            "runtime",
            "message.text",
        ):
            self.assertNotIn(forbidden, sanitizer.lower())

    def test_restore_does_not_advance_saved_at_without_interaction(self) -> None:
        saver = function_body(PRIVATE_CACHE, "saveResumeState")
        app_saver = function_body(APP, "saveMobileRestoreState")
        self.assertIn("advanceSavedAt", saver)
        self.assertIn("Number(input?.savedAt)", saver)
        self.assertIn('reason: "no_interaction"', saver)
        self.assertIn("advanceSavedAt: interaction", app_saver)
        self.assertIn("restoredSavedAt", app_saver)

    def test_offline_search_ui_is_explicit_and_online_switch_is_user_driven(self) -> None:
        for token in (
            'id="mobile-offline-search-banner"',
            "仅搜索已缓存内容",
            "搜索范围可能不完整，语义搜索不可用",
            "恢复网络后可搜索全部主机内容",
            'id="mobile-search-all-host"',
            "已缓存内容中没有匹配结果",
        ):
            self.assertIn(token, INDEX + APP)
        self.assertIn(".yunos-mobile-offline-search-banner", STYLE)
        self.assertIn(".yunos-mobile-cached-match", STYLE)
        switch = re.search(
            r'mobileSearchAllHost\?\.addEventListener\("click", async \(\) => \{(.+?)\n  \}\);',
            APP,
            re.DOTALL,
        )
        self.assertIsNotNone(switch)
        self.assertIn("forceOnline: true", switch.group(1))

    def test_cached_mode_never_uses_search_or_preview_api(self) -> None:
        search = function_body(APP, "runMobileSearch")
        preview = function_body(APP, "loadMobileContextPreview")
        self.assertIn("cache.searchCachedMessages", search)
        self.assertIn("if (useCached)", search)
        self.assertIn("if (hit.source_mode === \"cached-only\")", preview)
        cached_preview = preview.split(
            'if (hit.source_mode === "cached-only")', 1
        )[1].split(
            "if (mobileSearchState.previewAbortController)", 1
        )[0]
        self.assertIn("getCachedMessageContext", cached_preview)
        self.assertNotIn("/api/message-context", cached_preview)

    def test_state_save_triggers_cover_search_scroll_reader_and_lifecycle(self) -> None:
        initializer = function_body(APP, "initializeMobileSearch")
        for token in (
            "mobileResultsPage?.addEventListener(\"scroll\"",
            "mobileReaderScroll?.addEventListener(\"scroll\"",
            'window.addEventListener("pagehide"',
            'document.addEventListener("visibilitychange"',
            'window.addEventListener("orientationchange"',
            "saveMobileRestoreState",
        ):
            self.assertIn(token, initializer)
        self.assertIn("scheduleMobileRestoreStateSave", function_body(APP, "handleMobileSearchInput"))
        self.assertIn("scheduleMobileRestoreStateSave", function_body(APP, "toggleMobileContextPreview"))

    def test_service_worker_is_e3_static_only_and_api_network_only(self) -> None:
        self.assertIn('APP_SHELL_VERSION = "v10-e-agency-workspace-3"', SERVICE_WORKER)
        for resource in (
            '"/style.css?v=10-e-1"',
            '"/i18n/en.js?v=9-0-1-i18n-3"',
            '"/i18n/zh-CN.js?v=9-0-1-i18n-3"',
            '"/i18n/index.js?v=9-0-1-i18n-3"',
            '"/pwa.js?v=9-0-1-i18n-3"',
            '"/offline-cache.js?v=9-0-1-i18n-3"',
            '"/app.js?v=10-e-3"',
        ):
            self.assertIn(resource, SERVICE_WORKER)
        api_branch = SERVICE_WORKER.split(
            'if (url.pathname.startsWith("/api/"))', 1
        )[1].split('if (request.mode === "navigate")', 1)[0]
        self.assertIn("event.respondWith(fetch(request))", api_branch)
        self.assertNotIn("cache", api_branch.lower())
        self.assertNotIn('addEventListener("sync"', SERVICE_WORKER)
        self.assertNotIn('addEventListener("periodicsync"', SERVICE_WORKER.lower())

    def test_mobile_business_surface_remains_get_only_without_ai_or_media_cache(self) -> None:
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
            'putRecord("mediaObjects"',
            'putRecord("thumbnails"',
            "/api/runtime",
        ):
            self.assertNotIn(forbidden, mobile)


if __name__ == "__main__":
    unittest.main()
