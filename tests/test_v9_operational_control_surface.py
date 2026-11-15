from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OperationalControlSurfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        cls.app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        cls.style = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        cls.worker = (
            ROOT / "web" / "service-worker.js"
        ).read_text(encoding="utf-8")
        cls.server = (
            ROOT / "tools" / "local_web.py"
        ).read_text(encoding="utf-8")

    def test_formal_entries_are_exact_on_desktop_and_mobile(self) -> None:
        expected = ["当前", "项目", "等待", "决策", "委托"]
        block = re.search(
            r"operational:\s*\[(.*?)\n\s*\],\n\s*system:",
            self.app,
            re.DOTALL,
        )
        self.assertIsNotNone(block)
        desktop_labels = re.findall(r'fallback:\s*"([^"]+)"', block.group(1))
        self.assertEqual(desktop_labels, expected)
        self.assertEqual(
            re.findall(r'labelKey:\s*"([^"]+)"', block.group(1)),
            ["nav.now", "nav.projects", "nav.waiting", "nav.decisions", "nav.delegations"],
        )

        nav = re.search(
            r'id="mobile-operational-nav".*?</nav>',
            self.index,
            re.DOTALL,
        )
        self.assertIsNotNone(nav)
        mobile_labels = re.findall(r">([^<]+)</button>", nav.group(0))
        self.assertEqual(mobile_labels, expected)

    def test_operational_client_is_get_only_and_no_store(self) -> None:
        block = self.app.split(
            "const OPERATIONAL_VIEWS", 1
        )[1].split("const views =", 1)[0]
        self.assertIn('cache: "no-store"', self.app)
        self.assertNotRegex(block, r'method:\s*"(POST|PUT|PATCH|DELETE)"')
        self.assertNotIn("/api/operational-state/", self.worker)
        post_handler = re.search(
            r"    def do_POST\(.*?(?=\n    def )",
            self.server,
            re.DOTALL,
        )
        self.assertIsNotNone(post_handler)
        self.assertNotIn("operational-state", post_handler.group(0))

    def test_cache_disclosure_accessibility_and_responsive_contracts(self) -> None:
        self.assertIn('t("operational.cachedStaleReadonly"', self.app)
        self.assertIn("缓存 · 状态陈旧 · 只读", self.app)
        self.assertIn("result.cachedAt", self.app)
        self.assertIn(".yunos-mobile-operational-nav button", self.style)
        self.assertIn("min-height: 44px", self.style)
        self.assertIn(":focus-visible", self.style)
        self.assertIn("overflow-x: hidden", self.style)
        self.assertIn("prefers-reduced-motion: reduce", self.style)
        self.assertIn("data-mobile-view=\"operational\"", self.style)


if __name__ == "__main__":
    unittest.main()
