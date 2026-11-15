import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
STYLE = ROOT / "web" / "style.css"
APP = ROOT / "web" / "app.js"
LOCAL_WEB = ROOT / "tools" / "local_web.py"
RETRIEVER = ROOT / "memory" / "read_only_retriever.py"
BROWSER_OMNIBOX = ROOT / "tests" / "browser" / "v81_home_omnibox.js"


def _function_body(source: str, function_name: str) -> str:
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


def _css_rule(css: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}", css)
    if match is None:
        raise AssertionError(f"missing CSS rule for {selector}")
    return match.group("body")


class V81MemorySearchOverlayContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.css = STYLE.read_text(encoding="utf-8")
        cls.javascript = APP.read_text(encoding="utf-8")
        cls.local_web = LOCAL_WEB.read_text(encoding="utf-8")
        cls.retriever = RETRIEVER.read_text(encoding="utf-8")
        cls.browser_omnibox = BROWSER_OMNIBOX.read_text(encoding="utf-8")
        cls.home = cls.html.split('id="shell-home-region"', 1)[1].split(
            'id="shell-workspace-region"', 1
        )[0]

    def test_overlay_is_a_hidden_lightweight_list_below_the_omnibox(self) -> None:
        self.assertIn('id="home-memory-search-overlay"', self.home)
        self.assertIn('id="home-memory-search-results"', self.home)
        self.assertIn('role="listbox"', self.home)
        self.assertRegex(
            self.home,
            r'id="home-memory-search-overlay"[\s\S]{0,240}\bhidden\b',
        )
        self.assertIn('aria-controls="home-memory-search-results"', self.home)
        self.assertIn('aria-expanded="false"', self.home)
        self.assertLess(
            self.home.index('id="home-omnibox-form"'),
            self.home.index('id="home-memory-search-overlay"'),
        )
        overlay_css = _css_rule(self.css, ".yunos-home-memory-search-overlay")
        self.assertIn("position: absolute", overlay_css)
        self.assertRegex(overlay_css, r"top\s*:\s*calc\(100%\s*\+")
        self.assertIn("width: 100%", overlay_css)
        self.assertIn("max-height:", overlay_css)
        self.assertIn("overflow-y: auto", overlay_css)
        self.assertNotIn("100vw", overlay_css)
        self.assertNotRegex(self.home, r"(?i)(?:chat|assistant|user)[-_ ]?(?:bubble|message)")

    def test_only_explicit_search_mode_calls_the_existing_local_api(self) -> None:
        handler = _function_body(self.javascript, "handleHomeMemorySearchSubmit")
        self.assertIn('event.detail?.mode !== "search"', handler)
        self.assertIn("/api/search?q=", handler)
        self.assertIn("memory_space=whale", handler)
        self.assertIn("HOME_MEMORY_SEARCH_LIMIT", handler)
        self.assertRegex(
            self.javascript,
            r"HOME_MEMORY_SEARCH_LIMIT\s*=\s*8",
        )
        self.assertIn(
            'homeOmniboxForm?.addEventListener("yunos:home-omnibox-submit", '
            "handleHomeMemorySearchSubmit)",
            self.javascript,
        )
        self.assertNotRegex(
            handler,
            r"(?i)(?:openai|anthropic|deepseek|gemini|/api/runtime|/api/chat|https?://)",
        )
        self.assertIn("本地记忆搜索失败，请检查本地服务后重试。", handler)

    def test_real_result_fields_are_rendered_without_generated_content(self) -> None:
        renderer = _function_body(self.javascript, "renderHomeMemorySearchResults")
        renderer_and_anchor = renderer + _function_body(
            self.javascript, "homeMemorySearchAnchorLabel"
        )
        for field in (
            "item.title",
            "item.snippet",
            "item.create_time_iso",
            "item.update_time_iso",
            "item.source_platform",
            "item.source_platform_label",
            "item.message_id",
            "item.conversation_id",
        ):
            with self.subTest(field=field):
                self.assertIn(field, renderer_and_anchor)
        self.assertIn("homeMemorySearchAnchorLabel(item)", renderer)
        self.assertIn("buildReference(item)", renderer)
        self.assertNotRegex(renderer, r"(?i)generate|completion|prompt|model")

    def test_readability_tuning_keeps_four_to_five_compact_results_legible(self) -> None:
        overlay_css = _css_rule(self.css, ".yunos-home-memory-search-overlay")
        result_css = _css_rule(self.css, ".yunos-home-memory-search-result")
        title_css = _css_rule(self.css, ".yunos-home-memory-search-result-title")
        snippet_css = _css_rule(self.css, ".yunos-home-memory-search-result-snippet")
        meta_css = _css_rule(self.css, ".yunos-home-memory-search-result-meta")
        self.assertIn(
            "max-height: min(514px, calc(59.5vh - var(--yunos-shell-omnibox-height) - 18px))",
            overlay_css,
        )
        self.assertIn("scrollbar-width: thin", overlay_css)
        self.assertIn("padding: 11px 14px", result_css)
        self.assertIn("gap: 5px", result_css)
        self.assertIn("font-size: 15.5px", title_css)
        self.assertIn("font-size: 14.25px", snippet_css)
        self.assertIn("line-height: 1.48", snippet_css)
        self.assertIn("-webkit-line-clamp: 2", snippet_css)
        self.assertIn("font-size: 12.5px", meta_css)
        self.assertIn("line-height: 1.4", meta_css)
        renderer = _function_body(self.javascript, "renderHomeMemorySearchResults")
        self.assertIn("seenConversationIds", renderer)
        self.assertIn('is-repeated-conversation', renderer)
        self.assertIn("formatHomeMemorySearchTime", renderer)
        self.assertNotIn("labelSource(item.source)", renderer)

    def test_keyboard_selection_escape_and_accessibility_state_are_wired(self) -> None:
        for key in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
            with self.subTest(key=key):
                self.assertIn(f'event.key === "{key}"', self.javascript)
        self.assertIn('setAttribute("aria-activedescendant"', self.javascript)
        self.assertIn('setAttribute("aria-selected"', self.javascript)
        self.assertIn('setAttribute("aria-expanded"', self.javascript)
        self.assertIn("scrollIntoView({ block: \"nearest\" })", self.javascript)
        self.assertIn("closeHomeMemorySearchOverlay({ invalidateRequest: true })", self.javascript)

    def test_opening_a_result_reuses_conversation_detail_and_message_anchor(self) -> None:
        opener = _function_body(self.javascript, "openHomeMemorySearchResult")
        self.assertIn('setShellPageState("workspace")', opener)
        self.assertIn("loadConversation(item.conversation_id", opener)
        self.assertIn("focusMessageId: item.message_id", opener)
        loader = _function_body(self.javascript, "loadConversation")
        self.assertIn("focus_message_id=", loader)
        self.assertIn("renderConversation", loader)
        conversation = _function_body(self.javascript, "renderConversation")
        self.assertIn("options.focusMessageId", conversation)
        self.assertIn("返回首页搜索", conversation)
        self.assertIn("returnToHomeMemorySearch", conversation)
        return_home = _function_body(self.javascript, "returnToHomeMemorySearch")
        self.assertIn("window.history.replaceState", return_home)
        self.assertIn('setShellPageState("home")', return_home)

    def test_existing_search_contract_supplies_every_required_source_field(self) -> None:
        for assignment in (
            'item["source"]',
            'item["title"]',
            'item["update_time_iso"]',
            'item["conversation_id"]',
            'item["message_id"]',
            'item["reference_uri"]',
        ):
            with self.subTest(assignment=assignment):
                self.assertIn(assignment, self.local_web)
        self.assertIn('item["source_platform_label"]', self.local_web)
        self.assertIn('"snippet": text_snippet(', self.retriever)
        self.assertIn('"source_platform": row["source_platform"]', self.retriever)
        self.assertIn("mode=ro", self.retriever)

    def test_assets_are_cache_busted_for_the_current_node(self) -> None:
        self.assertIn('href="/style.css?v=10-e-1"', self.html)
        self.assertIn('src="/app.js?v=10-e-3"', self.html)

    def test_browser_regression_expects_local_search_and_review_only_ai_preview(self) -> None:
        self.assertIn('requestUrl.split("?")[0].endsWith("/api/search")', self.browser_omnibox)
        self.assertIn('requestUrl.split("?")[0].endsWith("/api/runtime-preview")', self.browser_omnibox)
        self.assertIn('requestUrl.startsWith(baseUrl)', self.browser_omnibox)
        self.assertIn('addedRequests.length === 1', self.browser_omnibox)
        self.assertIn('executeRequests.length === 0', self.browser_omnibox)


if __name__ == "__main__":
    unittest.main()
