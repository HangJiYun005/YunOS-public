import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
STYLE = ROOT / "web" / "style.css"
APP = ROOT / "web" / "app.js"
BROWSER = ROOT / "tests" / "browser" / "v81_contextual_sidebar.js"


class V81ContextualSidebarContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.css = STYLE.read_text(encoding="utf-8")
        cls.javascript = APP.read_text(encoding="utf-8")
        cls.browser = BROWSER.read_text(encoding="utf-8")

    def test_navigation_configuration_preserves_fourteen_unique_legacy_views(self) -> None:
        navigation = self.javascript.split("const SIDEBAR_NAVIGATION", 1)[1].split(
            "const shellSidebarState", 1
        )[0]
        views = re.findall(r'view:\s*"([^"]+)"', navigation)
        legacy_views = {
            "runtimeStatus",
            "runtimeExecution",
            "approvedProfiles",
            "recent",
            "search",
            "projects",
            "timeline",
            "summaries",
            "contextReview",
            "promptInspector",
            "contextOptimizer",
            "marks",
            "tags",
            "about",
        }
        self.assertEqual(len(set(views)), len(views))
        self.assertTrue(legacy_views.issubset(set(views)))
        for marker in (
            '{ key: "memory", labelKey: "nav.memory", fallback: "记忆", view: "search"',
            '{ key: "runtime", labelKey: "nav.runtime", fallback: "运行", view: "runtimeStatus"',
            'labelKey: "mobile.recentRecords", fallback: "最近记录", view: "recent"',
        ):
            self.assertIn(marker, navigation)

    def test_semantic_groups_and_low_priority_disclosures_are_declared(self) -> None:
        for marker in (
            'id="sidebar-global-items"',
            'id="sidebar-context-group"',
            'id="sidebar-system-items"',
            'id="sidebar-settings"',
            'id="sidebar-system-tools"',
        ):
            self.assertIn(marker, self.html)
        settings = self.html.split('id="sidebar-settings"', 1)[1].split("</details>", 1)[0]
        tools = self.html.split('id="sidebar-system-tools"', 1)[1].split("</details>", 1)[0]
        self.assertNotIn(" open", settings)
        self.assertNotIn(" open", tools)
        self.assertIn('class="yunos-sidebar-disclosure yunos-sidebar-tools"', self.html)

    def test_hover_and_pin_state_are_independent_and_testable(self) -> None:
        for marker in (
            'data-sidebar-pinned="false"',
            'data-sidebar-trigger="none"',
            "const SIDEBAR_HOVER_OPEN_DELAY_MS = 180",
            "const SIDEBAR_HOVER_CLOSE_DELAY_MS = 260",
            'trigger === "hover"',
            'trigger === "all"',
            'desktopShell.dataset.sidebarPinned = String(shellSidebarState.pinned)',
            'desktopShell.dataset.sidebarTrigger = trigger',
        ):
            self.assertIn(marker, self.html + self.javascript)
        self.assertIn('desktopShell.dataset.sidebarMode = pageState === "home" ? "overlay" : "push"', self.javascript)
        self.assertIn('if (pageState !== "home")', self.javascript)
        self.assertIn("shellSidebarState.hover = false", self.javascript)

    def test_context_is_derived_from_live_search_and_ai_state(self) -> None:
        context = self.javascript.split("function currentSidebarContextItems()", 1)[1].split(
            "function renderSidebarContext()", 1
        )[0]
        for marker in (
            "homeMemorySearchState.query",
            "homeMemorySearchState.open",
            "homeMemorySearchState.items.length",
            "homeAiState.question",
            "homeAiState.preview",
            "homeAiEvidenceItems().length",
            "homeAiProfileItems().length",
            "homeAiUncertaintyItems().length",
            't("status.reported", {}, "已记录")',
            't("common.preview", {}, "预览")',
        ):
            self.assertIn(marker, context)
        self.assertNotIn("Math.random", context)
        for section in ("sources", "memories", "uncertainties", "execution"):
            self.assertRegex(
                self.javascript,
                rf'false,\s*"{section}"',
            )

    def test_visual_contract_is_compact_and_continuous(self) -> None:
        root_css = self.css.split(":root {", 1)[1].split("}", 1)[0]
        self.assertIn("--yunos-shell-sidebar-width: clamp(208px, 13vw, 240px)", root_css)
        self.assertIn("--yunos-shell-nav-font-size: 14px", root_css)
        self.assertIn("width: 12px", self.css.split(".yunos-shell-edge-slot {", 1)[1].split("}", 1)[0])
        self.assertIn("width: 16px", self.css.split(".yunos-sidebar-icon {", 1)[1].split("}", 1)[0])
        self.assertIn("min-height: 32px", self.css)
        self.assertIn("font-size: 12px", self.css.split(".yunos-sidebar-group-title {", 1)[1].split("}", 1)[0])
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)

    def test_keyboard_outside_click_and_route_highlight_contracts_exist(self) -> None:
        for marker in (
            'document.addEventListener("pointerdown"',
            'document.addEventListener("keydown"',
            "!event.defaultPrevented",
            'event.key === "Escape"',
            'returnFocus: true',
            'item.setAttribute("aria-current", "page")',
            'setShellSidebarOpen(true, { trigger: "pinned" })',
        ):
            self.assertIn(marker, self.javascript)

    def test_browser_acceptance_covers_required_matrix_and_artifacts(self) -> None:
        for viewport in (
            "2560, height: 1440",
            "1920, height: 1080",
            "1707, height: 1067",
            "1440, height: 900",
            "1280, height: 720",
            "1100, height: 800",
        ):
            self.assertIn(viewport, self.browser)
        for artifact in (
            "01-home-sidebar-hidden",
            "02-home-hover-overlay",
            "03-home-click-pinned",
            "04-ai-result-push",
            "05-memory-search-context",
            "06-system-tools-collapsed",
            "07-system-tools-expanded",
            "08-workspace-push",
        ):
            self.assertIn(artifact, self.browser)
        for contract in (
            "overlay moved Omnibox",
            "workspace too narrow",
            "legacy view did not highlight",
            "reduced motion transition remained",
            "consoleErrors",
            "pageErrors",
            "failedResponses",
        ):
            self.assertIn(contract, self.browser)


if __name__ == "__main__":
    unittest.main()
