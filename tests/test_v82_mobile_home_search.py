import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
STYLE = ROOT / "web" / "style.css"
APP = ROOT / "web" / "app.js"
OFFLINE = ROOT / "web" / "offline-shell.html"
SERVICE_WORKER = ROOT / "web" / "service-worker.js"
LOCAL_WEB = ROOT / "tools" / "local_web.py"
RETRIEVER = ROOT / "memory" / "read_only_retriever.py"


def function_body(source: str, function_name: str) -> str:
    match = re.search(
        rf"(?:async\s+)?function\s+{re.escape(function_name)}\s*\([^)]*\)\s*\{{",
        source,
    )
    if match is None:
        raise AssertionError(f"missing function {function_name}")
    start = match.end() - 1
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(source)):
        character = source[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in ("'", '"', "`"):
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1 : index]
    raise AssertionError(f"unterminated function {function_name}")


class V82MobileHomeSearchContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.css = STYLE.read_text(encoding="utf-8")
        cls.javascript = APP.read_text(encoding="utf-8")
        cls.offline = OFFLINE.read_text(encoding="utf-8")
        cls.service_worker = SERVICE_WORKER.read_text(encoding="utf-8")
        cls.local_web = LOCAL_WEB.read_text(encoding="utf-8")
        cls.retriever = RETRIEVER.read_text(encoding="utf-8")
        cls.mobile_html = cls.html.split('id="yunos-mobile-shell"', 1)[1].split(
            'id="yunos-desktop-shell"', 1
        )[0]
        cls.mobile_javascript = cls.javascript.split(
            "function mobileVisibleCharacterCount", 1
        )[1].split("function splitTags", 1)[0]

    def test_mobile_home_is_an_independent_minimal_surface(self) -> None:
        for token in (
            'id="yunos-mobile-shell"',
            'data-mobile-view="home"',
            'data-search-position="center"',
            'class="yunos-mobile-brand">YunOS',
            'id="mobile-recent-button"',
            'id="mobile-search-form"',
            'id="mobile-search-input"',
            'enterkeyhint="search"',
            'id="mobile-results-page"',
            'id="mobile-recent-drawer"',
        ):
            self.assertIn(token, self.html)
        self.assertLess(
            self.html.index('id="yunos-mobile-shell"'),
            self.html.index('id="yunos-desktop-shell"'),
        )
        for forbidden in (
            "data-omnibox-mode",
            "home-ai-document",
            "shell-sidebar",
            "bottom-nav",
            "快捷",
            "推荐问题",
        ):
            self.assertNotIn(forbidden, self.mobile_html)

    def test_mobile_css_is_namespaced_safe_area_and_dynamic_viewport_aware(self) -> None:
        self.assertRegex(
            self.css,
            r"\.yunos-mobile-shell\s*\{[^}]*display:\s*none",
        )
        self.assertIn("@media (max-width: 767px), (max-width: 950px) and (max-height: 560px)", self.css)
        self.assertIn("#yunos-desktop-shell", self.css)
        self.assertIn("--yunos-mobile-visual-height", self.css)
        self.assertIn("--yunos-mobile-visual-width", self.css)
        self.assertIn("--yunos-mobile-visual-offset-top", self.css)
        self.assertIn("--yunos-mobile-visual-offset-left", self.css)
        self.assertIn("100dvh", self.css)
        for edge in ("top", "right", "bottom", "left"):
            self.assertIn(f"--yunos-safe-area-{edge}", self.css)
        self.assertIn("min-width: 44px", self.css)
        self.assertIn("min-height: 44px", self.css)
        self.assertIn("overflow-x: hidden", self.css)
        self.assertIn("-webkit-line-clamp: 3", self.css)

    def test_focus_keyboard_and_clear_restore_state_are_explicit(self) -> None:
        viewport = function_body(self.javascript, "syncMobileVisualViewport")
        position = function_body(self.javascript, "syncMobileSearchPosition")
        input_handler = function_body(self.javascript, "handleMobileSearchInput")
        initializer = function_body(self.javascript, "initializeMobileSearch")
        for property_name in ("height", "width", "offsetTop", "offsetLeft"):
            self.assertIn(f"visualViewport?.{property_name}", viewport)
        self.assertIn("visualViewport?.height ?? window.innerHeight", viewport)
        self.assertIn("visualViewport?.width ?? window.innerWidth", viewport)
        self.assertIn("visualViewport?.offsetTop ?? 0", viewport)
        self.assertIn("visualViewport?.offsetLeft ?? 0", viewport)
        self.assertIn('dataset.keyboardOpen = String(keyboardOpen)', viewport)
        self.assertIn("mobileSearchState.keyboardWasOpen = true", viewport)
        self.assertIn("mobileSearchState.keyboardDismissedWhileFocused = true", viewport)
        self.assertIn("inputFocused && !mobileSearchState.keyboardDismissedWhileFocused", position)
        self.assertIn('dataset.searchPosition = shouldPin ? "top" : "center"', position)
        self.assertIn('window.visualViewport?.addEventListener("resize", syncMobileVisualViewport)', self.javascript)
        self.assertIn('window.visualViewport?.addEventListener("scroll", syncMobileVisualViewport)', self.javascript)
        self.assertIn("window.requestAnimationFrame(syncMobileVisualViewport)", initializer)
        self.assertNotIn("window.scrollTo", self.mobile_javascript)
        self.assertNotIn("setTimeout(syncMobileVisualViewport, 50)", initializer)
        self.assertNotIn("setTimeout(syncMobileVisualViewport, 150)", initializer)
        self.assertIn('setMobileView("home")', input_handler)
        self.assertIn("resetMobileSearchResults()", input_handler)
        self.assertIn(
            'mobileSearchClear?.addEventListener("click", () => {\n'
            '    mobileSearchInput.value = "";\n'
            '    handleMobileSearchInput();\n'
            '    mobileSearchInput.blur();',
            self.mobile_javascript,
        )
        self.assertIn("transition: transform 220ms", self.css)
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        self.assertIn("#mobile-search-input:focus,", self.css)
        self.assertIn("#mobile-search-input:focus-visible {", self.css)
        self.assertIn("outline: 0;", self.css)
        self.assertIn("outline: 1px solid color-mix", self.css)
        self.assertIn("outline-offset: -2px", self.css)

    def test_debounce_minimum_abort_dedup_and_ios_submit_contract(self) -> None:
        self.assertRegex(self.javascript, r"MOBILE_SEARCH_DEBOUNCE_MS\s*=\s*1000")
        self.assertRegex(self.javascript, r"MOBILE_SEARCH_PAGE_SIZE\s*=\s*20")
        self.assertRegex(self.javascript, r"MOBILE_SEARCH_AUTO_LIMIT\s*=\s*60")
        self.assertIn("再输入一个字开始搜索", self.javascript)
        self.assertIn("new AbortController()", self.mobile_javascript)
        self.assertIn('error?.name === "AbortError"', function_body(self.javascript, "api"))
        runner = function_body(self.javascript, "runMobileSearch")
        self.assertIn("visibleCharacters < 2", runner)
        self.assertIn("activeRequestKey === requestKey", runner)
        self.assertIn("completedFirstPageKey === firstPageKey", runner)
        self.assertIn('mobileSearchForm.addEventListener("submit"', self.mobile_javascript)
        for token in ('scope: "all"', 'aggregate: "conversation"', "offset:", "limit:", "sort:"):
            self.assertIn(token, function_body(self.javascript, "mobileSearchUrl"))

    def test_results_sort_aggregation_and_highlight_contract(self) -> None:
        normalizer = function_body(self.javascript, "normalizeMobileSearchGroup")
        renderer = function_body(self.javascript, "mobileSearchGroupElement")
        expander = function_body(self.javascript, "setExpandedMobileConversation")
        self.assertIn("item.best_hit", normalizer)
        self.assertIn("item.other_hits", normalizer)
        self.assertIn("other_match_count", normalizer)
        hit_renderer = function_body(self.javascript, "mobileHitBlock")
        self.assertIn("appendHighlighted", hit_renderer)
        self.assertIn("cleanMobileSearchSnippet", hit_renderer)
        self.assertIn(r".replace(/\u0060{3}", self.javascript)
        self.assertIn(r".replace(/\u0060+/gu", self.javascript)
        self.assertIn(r".replace(/!\[", self.javascript)
        self.assertIn(r"(?:#{1,6}|>{1,3})", self.javascript)
        self.assertIn(r".replace(/\|+/gu", self.javascript)
        self.assertIn(r"(?:\*{2,}|_{2,}|~{2,})", self.javascript)
        self.assertIn(r"replace(/\s+/gu", self.javascript)
        self.assertIn("相关匹配", function_body(self.javascript, "mobileHitBlock"))
        self.assertIn(
            "另有 ${formatUiNumber(group.other_match_count)} 处匹配",
            renderer,
        )
        self.assertIn('"search.moreMatches"', renderer)
        self.assertIn("expandedConversationId = conversationId", expander)
        self.assertIn('querySelectorAll(".yunos-mobile-result")', expander)
        self.assertIn("相关度优先", self.mobile_javascript)
        self.assertIn("最新优先", self.mobile_javascript)
        self.assertIn("runMobileSearch({ reset: true })", self.mobile_javascript)

    def test_continuous_loading_stops_automatic_work_at_sixty(self) -> None:
        controls = function_body(self.javascript, "updateMobileLoadControls")
        next_page = function_body(self.javascript, "loadNextMobileSearchPage")
        initializer = function_body(self.javascript, "initializeMobileSearch")
        self.assertIn("count >= MOBILE_SEARCH_AUTO_LIMIT", controls)
        self.assertIn("重新加载", controls)
        self.assertIn("加载更多", controls)
        self.assertIn("groups.length >= MOBILE_SEARCH_AUTO_LIMIT", next_page)
        self.assertIn("IntersectionObserver", initializer)
        self.assertIn("mobileSearchState.autoLoadPaused", initializer)
        self.assertIn("mobileSearchState.paginationError", initializer)

    def test_recent_drawer_is_local_lightweight_and_draggable(self) -> None:
        self.assertIn('MOBILE_SEARCH_HISTORY_KEY = "yunos.mobileSearch.history.v1"', self.javascript)
        self.assertIn("window.localStorage", self.mobile_javascript)
        self.assertIn("searches", function_body(self.javascript, "readMobileHistory"))
        self.assertIn("conversations", function_body(self.javascript, "readMobileHistory"))
        self.assertIn("deleteMobileHistoryItem", self.mobile_javascript)
        self.assertIn("writeMobileHistory({ searches: [], conversations: [] })", self.mobile_javascript)
        self.assertIn('target.querySelector(".yunos-mobile-recent-heading")', function_body(self.javascript, "renderMobileHistoryList"))
        self.assertIn("dragDistance >= 72", function_body(self.javascript, "initializeMobileSearch"))
        self.assertIn("height: min(60dvh", self.css)
        self.assertIn("--yunos-mobile-drawer-drag-y", self.css)

    def test_mobile_node_has_no_ai_write_or_offline_content_cache(self) -> None:
        for forbidden in (
            "/api/runtime",
            'method: "POST"',
            "indexedDB",
            "caches.open",
            "Tailscale",
        ):
            self.assertNotIn(forbidden, self.mobile_javascript)
        api_branch = self.service_worker.split('if (url.pathname.startsWith("/api/"))', 1)[1].split(
            'if (request.mode === "navigate")', 1
        )[0]
        self.assertIn("event.respondWith(fetch(request))", api_branch)
        self.assertNotIn("cache", api_branch.lower())

    def test_desktop_v81_dom_and_events_remain_available(self) -> None:
        for token in (
            'id="yunos-desktop-shell"',
            'id="shell-sidebar-toggle"',
            'id="shell-sidebar"',
            'id="home-omnibox-form"',
            'id="home-memory-search-overlay"',
            'id="content"',
            'id="view-title"',
            'id="db-mode"',
            'id="db-warning"',
        ):
            self.assertIn(token, self.html)
        self.assertIn('homeOmniboxForm?.addEventListener("yunos:home-omnibox-submit"', self.javascript)
        self.assertIn("memory_space=whale", function_body(self.javascript, "handleHomeMemorySearchSubmit"))
        self.assertIn("restoreHomeAiSession", self.javascript)
        self.assertIn("isMobileSearchSurfaceActive() ? undefined", self.javascript)

    def test_pwa_assets_are_versioned_together(self) -> None:
        unchanged = "9-0-1-i18n-3"
        style_revision = "10-e-1"
        script_revision = "10-e-3"
        self.assertIn(f'href="/style.css?v={style_revision}"', self.html)
        self.assertIn(f'src="/pwa.js?v={unchanged}"', self.html)
        self.assertIn(f'src="/offline-cache.js?v={unchanged}"', self.html)
        self.assertIn(f'src="/app.js?v={script_revision}"', self.html)
        self.assertIn(f'href="/style.css?v={style_revision}"', self.offline)
        self.assertIn(
            'APP_SHELL_VERSION = "v10-e-agency-workspace-3"',
            self.service_worker,
        )
        for resource in (
            f'"/style.css?v={style_revision}"',
            f'"/i18n/en.js?v={unchanged}"',
            f'"/i18n/zh-CN.js?v={unchanged}"',
            f'"/i18n/index.js?v={unchanged}"',
            f'"/pwa.js?v={unchanged}"',
            f'"/offline-cache.js?v={unchanged}"',
            f'"/app.js?v={script_revision}"',
        ):
            self.assertIn(resource, self.service_worker)
        self.assertIn("cacheName.startsWith(APP_SHELL_CACHE_PREFIX)", self.service_worker)
        self.assertIn(".map((cacheName) => caches.delete(cacheName))", self.service_worker)

    def test_enhanced_api_stays_explicit_get_only_and_read_only(self) -> None:
        search_handler = self.local_web.split("    def api_search", 1)[1].split(
            "    def search_item_payload", 1
        )[0]
        self.assertIn('scope == "all" and aggregate == "conversation"', search_handler)
        self.assertIn("search_all_memory_conversations", search_handler)
        self.assertIn('f"file:{path.as_posix()}?mode=ro"', self.retriever)
        enhanced = self.retriever.split("def search_all_memory_conversations", 1)[1].split(
            "\ndef retrieve(", 1
        )[0]
        for forbidden in ("INSERT", "UPDATE", "DELETE", "commit(", "use_embedding"):
            self.assertNotIn(forbidden, enhanced)


if __name__ == "__main__":
    unittest.main()
