import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
STYLE = ROOT / "web" / "style.css"
APP = ROOT / "web" / "app.js"
BROWSER_CONTINUITY = ROOT / "tests" / "browser" / "v81_responsive_continuity.js"


class V81DesktopShellFoundationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.css = STYLE.read_text(encoding="utf-8")
        cls.javascript = APP.read_text(encoding="utf-8")
        cls.browser_continuity = BROWSER_CONTINUITY.read_text(encoding="utf-8")

    def test_root_shell_exposes_low_coupling_layout_contract(self) -> None:
        self.assertIn('id="yunos-desktop-shell"', self.html)
        self.assertIn('data-page-state="home"', self.html)
        self.assertIn('data-sidebar-mode="overlay"', self.html)
        self.assertIn('data-sidebar-open="false"', self.html)
        self.assertIn('data-shell-slot="logo"', self.html)
        self.assertEqual(self.html.count('id="shell-sidebar-toggle"'), 1)
        self.assertNotIn('id="shell-home-button"', self.html)
        self.assertIn('class="yunos-shell-logo-word" aria-hidden="true">YunOS</span>', self.html)
        self.assertIn('class="yunos-shell-menu-icon" aria-hidden="true"', self.html)
        self.assertIn('aria-controls="shell-sidebar"', self.html)
        self.assertIn('aria-expanded="false"', self.html)
        self.assertIn('data-shell-slot="sidebar-edge"', self.html)
        self.assertIn('data-shell-region="home"', self.html)
        self.assertIn('data-shell-region="workspace"', self.html)
        self.assertEqual(self.html.count("<main"), 1)
        self.assertRegex(self.html, r'<main\s+class="yunos-shell-stage"')

    def test_home_anchor_hosts_the_v81_b_omnibox_without_extra_home_content(self) -> None:
        home_markup = self.html.split('id="shell-home-region"', 1)[1].split("</section>", 1)[0]
        self.assertIn('data-shell-slot="omnibox"', home_markup)
        self.assertIn('id="home-omnibox-form"', home_markup)
        self.assertIn('id="home-omnibox-input"', home_markup)
        self.assertEqual(len(re.findall(r'<input\b', home_markup)), 1)
        self.assertIn('data-omnibox-mode="search"', home_markup)
        self.assertIn('data-omnibox-mode="ai"', home_markup)
        for forbidden in ("今天想聊", "推荐问题", "上传", "语音", 'data-omnibox-mode="auto"'):
            self.assertNotIn(forbidden, home_markup)

    def test_existing_frontend_mounts_and_all_legacy_entries_remain(self) -> None:
        for marker in ('id="content"', 'id="view-title"', 'id="db-mode"', 'id="db-warning"'):
            self.assertIn(marker, self.html)
        navigation = self.javascript.split("const SIDEBAR_NAVIGATION", 1)[1].split(
            "const shellSidebarState", 1
        )[0]
        views = re.findall(r'view:\s*"([^"]+)"', navigation)
        self.assertEqual(len(views), len(set(views)))
        required_views = {
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
        self.assertTrue(required_views.issubset(set(views)))
        for view in required_views:
            self.assertRegex(self.javascript, rf"\b{re.escape(view)}\s*:")

    def test_design_tokens_and_three_desktop_bands_are_explicit(self) -> None:
        self.assertIn('href="/style.css?v=10-e-1"', self.html)
        for token in (
            "--yunos-canvas",
            "--yunos-surface",
            "--yunos-border",
            "--yunos-text",
            "--yunos-shell-sidebar-width",
            "--yunos-shell-content-max",
            "--yunos-shell-omnibox-width",
            "--yunos-shell-logo-font-size",
            "--yunos-shell-nav-font-size",
        ):
            self.assertIn(token, self.css)
        self.assertIn("@media (width >= 1800px)", self.css)
        self.assertIn("@media (1360px <= width < 1800px)", self.css)
        self.assertIn("@media (width < 1360px)", self.css)
        self.assertNotIn("max-width: 1799px", self.css)
        self.assertNotIn("max-width: 1359.98px", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)

    def test_a2_fluid_scale_contract_preserves_approved_endpoints(self) -> None:
        root_css = self.css.split(":root {", 1)[1].split("}", 1)[0]
        wide_css = self.css.split("@media (width >= 1800px)", 1)[1].split(
            "@media (1360px <= width < 1800px)", 1
        )[0]
        compact_css = self.css.split(
            "@media (1360px <= width < 1800px)", 1
        )[1].split("@media (width < 1360px)", 1)[0]
        narrow_css = self.css.split("@media (width < 1360px)", 1)[1].split(
            "@media (max-width: 980px)", 1
        )[0]
        anchor_css = self.css.split(".yunos-shell-omnibox-anchor {", 1)[1].split("}", 1)[0]

        self.assertIn(
            "--yunos-shell-omnibox-width: clamp(580px, calc(28.604924vw + 131.713951px), 864px)",
            root_css,
        )
        self.assertIn(
            "--yunos-shell-omnibox-height: clamp(52px, calc(1.547479vw + 27.584525px), 67.2px)",
            root_css,
        )
        self.assertIn(
            "--yunos-shell-logo-font-size: clamp(19px, calc(1.113716vw - 0.011137px), 28.5px)",
            root_css,
        )
        self.assertIn(
            "--yunos-shell-sidebar-width: clamp(208px, 13vw, 240px)",
            root_css,
        )
        self.assertIn("--yunos-shell-nav-font-size: 14px", root_css)
        for band_css in (wide_css, compact_css, narrow_css):
            self.assertNotIn("--yunos-shell-omnibox-width", band_css)
            self.assertNotIn("--yunos-shell-omnibox-height", band_css)
            self.assertNotIn(".yunos-shell-logo-word", band_css)
            self.assertNotIn(".nav-item", band_css)
        self.assertIn("top: 41.5%", anchor_css)
        self.assertIn("border-radius: 999px", anchor_css)

    def test_a2_typography_tuning_has_clear_minimums(self) -> None:
        logo_css = self.css.split(".yunos-shell-logo-word {", 1)[1].split("}", 1)[0]
        nav_css = self.css.split(".yunos-shell-sidebar .yunos-sidebar-item {", 1)[1].split("}", 1)[0]

        self.assertIn("font-size: var(--yunos-shell-logo-font-size)", logo_css)
        self.assertIn("grid-template-columns: 16px minmax(0, 1fr)", nav_css)
        self.assertIn("font-size: 14px", self.css.split(".yunos-shell-sidebar .yunos-sidebar-item,", 1)[1].split("}", 1)[0])

    def test_logo_and_sidebar_toggle_are_one_fixed_crossfade_control(self) -> None:
        control_css = self.css.split(".yunos-shell-brand-control {", 1)[1].split("}", 1)[0]
        logo_css = self.css.split(".yunos-shell-logo-word {", 1)[1].split("}", 1)[0]
        menu_css = self.css.split(".yunos-shell-menu-icon {", 1)[1].split("}", 1)[0]

        self.assertIn("position: relative", control_css)
        self.assertIn("width: 88px", control_css)
        self.assertIn("height: 40px", control_css)
        self.assertIn("flex: 0 0 88px", control_css)
        self.assertIn("display: grid", control_css)
        self.assertIn('grid-template-areas: "brand"', control_css)
        self.assertIn("justify-content: start", control_css)
        self.assertIn("grid-area: brand", logo_css)
        self.assertIn("opacity: 1", logo_css)
        self.assertIn("transition: opacity var(--yunos-motion-fast)", logo_css)
        self.assertIn("position: relative", menu_css)
        self.assertIn("grid-area: brand", menu_css)
        self.assertIn("justify-self: center", menu_css)
        self.assertIn("opacity: 0", menu_css)
        self.assertIn("transition: opacity var(--yunos-motion-fast)", menu_css)
        self.assertRegex(
            self.css,
            r"\.yunos-shell-menu-icon::before,\s*\.yunos-shell-menu-icon::after\s*"
            r"\{[^}]*position:\s*absolute",
        )
        self.assertIn('.yunos-shell-brand-control[aria-expanded="true"] .yunos-shell-logo-word', self.css)
        self.assertIn('.yunos-shell-brand-control[aria-expanded="true"] .yunos-shell-menu-icon', self.css)
        self.assertNotIn("shellHomeButton", self.javascript)

    def test_muted_text_contrast_meets_normal_text_threshold(self) -> None:
        muted_match = re.search(r"--yunos-muted:\s*(#[0-9a-fA-F]{6})", self.css)
        self.assertIsNotNone(muted_match)

        def luminance(hex_color: str) -> float:
            channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [
                value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                for value in channels
            ]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def contrast(first: str, second: str) -> float:
            lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
            return (lighter + 0.05) / (darker + 0.05)

        for background in ("#ffffff", "#fbfcfd", "#f7f8fa"):
            self.assertGreaterEqual(contrast(muted_match.group(1), background), 4.5)

    def test_real_browser_regression_covers_boundary_layout_behavior(self) -> None:
        for viewport in ("1359", "1707", "1798", "1799", "1800", "1801", "2560"):
            self.assertIn(viewport, self.browser_continuity)
        for browser_contract in (
            "setViewportSize",
            "1799.49",
            "matchMedia",
            "scrollWidth",
            "console",
            "pageerror",
            "pageState",
            "sidebarMode",
        ):
            self.assertIn(browser_contract, self.browser_continuity)

    def test_shell_adapter_switches_only_page_and_sidebar_layout_state(self) -> None:
        self.assertIn('desktopShell.dataset.pageState = pageState', self.javascript)
        self.assertIn('desktopShell.dataset.sidebarMode = pageState === "home" ? "overlay" : "push"', self.javascript)
        self.assertIn('desktopShell.dataset.sidebarOpen = String(isOpen)', self.javascript)
        self.assertIn('shellSidebar.toggleAttribute("inert", !isOpen)', self.javascript)


if __name__ == "__main__":
    unittest.main()
