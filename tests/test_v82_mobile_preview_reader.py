import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
STYLE = ROOT / "web" / "style.css"
APP = ROOT / "web" / "app.js"
LOCAL_WEB = ROOT / "tools" / "local_web.py"
READER = ROOT / "memory" / "conversation_reader.py"
SERVICE_WORKER = ROOT / "web" / "service-worker.js"


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


class V82MobilePreviewReaderContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.css = STYLE.read_text(encoding="utf-8")
        cls.javascript = APP.read_text(encoding="utf-8")
        cls.local_web = LOCAL_WEB.read_text(encoding="utf-8")
        cls.reader = READER.read_text(encoding="utf-8")
        cls.service_worker = SERVICE_WORKER.read_text(encoding="utf-8")

    def test_reader_is_a_dedicated_mobile_surface(self) -> None:
        for token in (
            'id="mobile-conversation-reader"',
            'data-topbar-visible="true"',
            'id="mobile-reader-back"',
            'id="mobile-reader-title"',
            'id="mobile-reader-hide-topbar"',
            'id="mobile-reader-restore-topbar"',
            'id="mobile-reader-scroll"',
            'id="mobile-reader-messages"',
        ):
            self.assertIn(token, self.html)
        self.assertLess(
            self.html.index('id="mobile-conversation-reader"'),
            self.html.index('id="yunos-desktop-shell"'),
        )
        self.assertIn('nextView = view === "reader"', function_body(self.javascript, "setMobileView"))
        self.assertNotIn("renderConversation(", function_body(self.javascript, "openMobileConversationReader"))

    def test_result_body_source_and_preview_are_separate_large_targets(self) -> None:
        unit = function_body(self.javascript, "mobileResultHitUnit")
        self.assertIn("mobileHitBlock", unit)
        self.assertIn("mobileResultSourceButton", unit)
        self.assertIn("unit.append(body, preview, source)", unit)
        self.assertIn("toggleMobileContextPreview", unit)
        self.assertIn("openMobileConversationReader", unit)
        self.assertRegex(
            self.css,
            r"\.yunos-mobile-result-body\s*\{[^}]*min-height:\s*72px",
        )
        self.assertRegex(
            self.css,
            r"\.yunos-mobile-result-unit \.yunos-mobile-result-source\s*\{[^}]*min-height:\s*48px",
        )

    def test_context_preview_uses_exact_composite_anchor_and_window_two(self) -> None:
        loader = function_body(self.javascript, "loadMobileContextPreview")
        self.assertIn("conversation_id: hit.conversation_id", loader)
        self.assertIn("message_id: hit.message_id", loader)
        self.assertIn('window: "2"', loader)
        self.assertIn("/api/message-context?", loader)
        toggle = function_body(self.javascript, "toggleMobileContextPreview")
        self.assertIn("expandedPreviewKey === key", toggle)
        self.assertIn("syncMobileExpandedPreview", toggle)
        self.assertIn("afterTop - beforeTop", toggle)
        self.assertIn("user-select: text", self.css)
        self.assertIn("width: min(82%", self.css)

    def test_reader_is_anchor_centered_and_bidirectionally_segmented(self) -> None:
        url = function_body(self.javascript, "mobileConversationUrl")
        self.assertIn("/api/conversation-messages", url)
        self.assertIn('params.set("before"', url)
        self.assertIn('params.set("after"', url)
        self.assertIn('params.set("anchor_message_id"', url)
        loader = function_body(self.javascript, "loadMobileConversationSegment")
        self.assertIn("MOBILE_READER_PAGE_SIZE", self.javascript)
        self.assertIn("mobileReaderMessages?.prepend(fragment)", loader)
        self.assertIn("mobileReaderMessages?.appendChild(fragment)", loader)
        self.assertIn("afterTop - anchorTop", loader)
        self.assertIn("beforeError", loader)
        self.assertIn("afterError", loader)
        self.assertNotIn('method: "POST"', loader)

    def test_trace_selection_topbar_and_back_state_are_explicit(self) -> None:
        trace = function_body(self.javascript, "toggleMobileReaderTrace")
        self.assertIn("window.getSelection", trace)
        self.assertIn("!selection.isCollapsed", trace)
        self.assertIn("expandedTraceIds", trace)
        topbar = function_body(self.javascript, "setMobileReaderTopbarVisible")
        self.assertIn("topbarVisibility", topbar)
        self.assertIn("mobileReaderRestoreTopbar.hidden", topbar)
        open_reader = function_body(self.javascript, "openMobileConversationReader")
        self.assertIn("history.replaceState", open_reader)
        self.assertIn("history.pushState", open_reader)
        self.assertIn("resultsScrollTop", open_reader)
        back = function_body(self.javascript, "showMobileSearchResultsFromReader")
        self.assertIn("setMobileView(\"results\")", back)
        self.assertNotIn("runMobileSearch", back)
        self.assertNotIn("renderMobileSearchResults", back)
        self.assertIn("right: max(8px", self.css)
        self.assertIn("touch-action: pan-x pan-y pinch-zoom", self.css)

    def test_new_api_is_get_only_read_only_and_old_desktop_contract_remains(self) -> None:
        dispatch = self.local_web.split("    def handle_api_get", 1)[1].split(
            "    def handle_api_post", 1
        )[0]
        self.assertIn('path == "/api/conversation-messages"', dispatch)
        self.assertIn("read_conversation_messages", self.local_web)
        self.assertIn("mode=ro", self.reader)
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", ".commit("):
            self.assertNotIn(forbidden, self.reader.upper() if forbidden.isupper() else self.reader)
        legacy = self.local_web.split("    def api_conversation(self", 1)[1].split(
            "    def api_conversation_messages", 1
        )[0]
        self.assertIn("ORDER BY ordinal ASC, create_time ASC", legacy)
        self.assertIn('"messages": [self.message_payload', legacy)
        api_branch = self.service_worker.split('if (url.pathname.startsWith("/api/"))', 1)[1].split(
            'if (request.mode === "navigate")', 1
        )[0]
        self.assertIn("event.respondWith(fetch(request))", api_branch)
        self.assertNotIn("cache", api_branch.lower())


if __name__ == "__main__":
    unittest.main()
