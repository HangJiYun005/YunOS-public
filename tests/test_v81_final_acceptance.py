import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
STYLE = ROOT / "web" / "style.css"
APP = ROOT / "web" / "app.js"
BROWSER = ROOT / "tests" / "browser" / "v81_final_acceptance.js"
REAL_BROWSER = ROOT / "tests" / "browser" / "v81_real_deepseek_acceptance.js"
RUNTIME_CONTRACTS = ROOT / "memory" / "runtime_protocol_contracts.py"


class V81FinalAcceptanceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.css = STYLE.read_text(encoding="utf-8")
        cls.javascript = APP.read_text(encoding="utf-8")
        cls.browser = BROWSER.read_text(encoding="utf-8")
        cls.real_browser = REAL_BROWSER.read_text(encoding="utf-8")
        cls.runtime_contracts = RUNTIME_CONTRACTS.read_text(encoding="utf-8")

    def test_f_assets_are_cache_busted_and_compatibility_splash_is_nonblocking(self) -> None:
        self.assertIn('href="/style.css?v=10-e-1"', self.html)
        self.assertIn('src="/app.js?v=10-e-3"', self.html)
        splash = self.html.split('id="splash"', 1)[1].split(">", 1)[0]
        self.assertIn("hidden", splash)
        self.assertIn('aria-hidden="true"', splash)
        self.assertNotIn("点击任意位置进入", self.html)
        self.assertNotIn("splash-title", self.html)
        self.assertIn("const AUTO_ENTER_DELAY = 0", self.javascript)
        self.assertIn("const ENTER_ON_ANY_CLICK = false", self.javascript)

    def test_focus_and_low_height_scroll_contracts_are_shared(self) -> None:
        for token in (
            "--yunos-focus: #355f86",
            "outline: 2px solid var(--yunos-focus)",
            "scrollbar-color: var(--yunos-scrollbar) transparent",
            "scrollbar-gutter: stable",
            "@media (max-height: 820px)",
            ".sidebar::-webkit-scrollbar",
            "padding: 16px 12px max(22px, env(safe-area-inset-bottom))",
        ):
            self.assertIn(token, self.css)
        self.assertNotIn("outline: none", self.css)

    def test_legacy_clickable_cards_are_keyboard_activatable(self) -> None:
        helper = self.javascript.split("function makeKeyboardActivatable", 1)[1].split(
            "function conversationCard", 1
        )[0]
        for token in (
            'element.setAttribute("role", "link")',
            "element.tabIndex = 0",
            'event.key !== "Enter"',
            'event.key !== " "',
            "event.preventDefault()",
        ):
            self.assertIn(token, helper)
        self.assertEqual(4, self.javascript.count("makeKeyboardActivatable("))
        self.assertIn(".card.clickable:focus-visible", self.css)

    def test_runtime_protocol_is_visible_without_frontend_override(self) -> None:
        self.assertIn(
            'FIXED_GENERATION_CONFIG = {"temperature": 0.0, "max_tokens": 1024}',
            self.runtime_contracts,
        )
        execute = self.javascript.split("async function executeHomeAiDocumentOnce", 1)[1].split(
            "function cancelHomeAiDocument", 1
        )[0]
        self.assertNotIn("max_tokens", execute)

    def test_final_browser_matrix_covers_release_scope(self) -> None:
        for viewport in (
            "2560, height: 1440",
            "1920, height: 1080",
            "1707, height: 1067",
            "1600, height: 900",
            "1440, height: 900",
            "1280, height: 720",
            "1100, height: 800",
        ):
            self.assertIn(viewport, self.browser)
        for token in (
            "continuousWidths",
            "1000",
            "2560",
            "memory-search-keyboard",
            "message anchor",
            "ai-review-only-preview",
            "runtime-execute",
            "requiredViews",
            "sidebar-low-height-scroll",
            "externalRequests",
            "consoleErrors",
            "pageErrors",
            "failedResponses",
        ):
            self.assertIn(token, self.browser)

    def test_real_provider_acceptance_is_persistent_single_shot_and_salvage_only(self) -> None:
        source = self.real_browser
        self.assertEqual(1, source.count("await executeButton.click()"))
        post_click = source.split("await executeButton.click()", 1)[1]
        self.assertNotIn("throw new Error", post_click)
        self.assertNotIn("removeItem(oneShotSentryKey)", source)
        self.assertIn("persistent one-shot sentry already exists; DO NOT EXECUTE OR RERUN", source)
        self.assertIn("CLICK_DISPATCHED_DO_NOT_RERUN", source)
        self.assertIn("doNotAutoClear: true", source)
        self.assertIn("measuredClickEventCount", source)
        self.assertIn("NO_GO_DO_NOT_RERUN", source)
        self.assertIn("CALL_DISPATCH_UNKNOWN_DO_NOT_RERUN", source)
        self.assertIn("Never throw after the click boundary", source)
        self.assertIn("fixedGenerationConfig: expectedGenerationConfig", source)
        self.assertIn("max_tokens: 1024", source)
        self.assertIn("expectedSuccessLedgerDelta", source)
        self.assertIn("expectedRuntimeEventSequence", source)
        self.assertIn("directRuntimeEventReadbackArtifactRequired: true", source)
        self.assertIn("requestId: preview.request_id", source)
        self.assertIn("mutationId: preview.mutation_id", source)
        self.assertIn("reviewedArtifactIdentity", source)
        self.assertIn("projectionIdentity", source)
        self.assertIn("evidenceReferenceId", source)


if __name__ == "__main__":
    unittest.main()
