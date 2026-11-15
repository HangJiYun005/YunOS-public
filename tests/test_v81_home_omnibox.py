import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
STYLE = ROOT / "web" / "style.css"
APP = ROOT / "web" / "app.js"


def _attributes(tag: str) -> dict[str, str]:
    return {
        name.lower(): value
        for name, _, value in re.findall(
            r"([:\w-]+)\s*=\s*(['\"])(.*?)\2", tag, flags=re.DOTALL
        )
    }


def _css_rule(css: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}", css)
    if match is None:
        raise AssertionError(f"missing CSS rule for {selector}")
    return match.group("body")


def _listener_bodies(javascript: str, event_name: str) -> list[str]:
    """Return balanced callback bodies for addEventListener(event_name, ...)."""
    pattern = re.compile(
        rf"\.addEventListener\(\s*(['\"]){re.escape(event_name)}\1\s*,",
        flags=re.IGNORECASE,
    )
    bodies: list[str] = []
    for match in pattern.finditer(javascript):
        start = javascript.find("{", match.end())
        if start < 0:
            continue
        depth = 0
        quote = ""
        escaped = False
        for index in range(start, len(javascript)):
            character = javascript[index]
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
                    bodies.append(javascript[start + 1 : index])
                    break
    return bodies


class V81HomeOmniboxContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.css = STYLE.read_text(encoding="utf-8")
        cls.javascript = APP.read_text(encoding="utf-8")
        cls.home = cls.html.split('id="shell-home-region"', 1)[1].split(
            "</section>", 1
        )[0]

    def test_home_slot_contains_a_real_single_line_form_control(self) -> None:
        self.assertIn('data-shell-slot="omnibox"', self.home)
        self.assertRegex(
            self.home,
            r"<form\b(?=[^>]*\bid=['\"]home-omnibox-form['\"])(?=[^>]*\bclass=['\"][^'\"]*\byunos-home-omnibox\b)",
        )
        input_match = re.search(
            r"<input\b(?=[^>]*\bid=['\"]home-omnibox-input['\"])[^>]*>",
            self.home,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(input_match)
        input_attributes = _attributes(input_match.group(0))
        self.assertIn(input_attributes.get("type", "text").lower(), {"text", "search"})
        self.assertNotIn("multiple", input_attributes)
        self.assertNotRegex(self.home, r"<(?:textarea|select)\b")
        self.assertNotIn('aria-hidden="true"', self.home)

    def test_capsule_reuses_the_existing_fluid_size_tokens(self) -> None:
        anchor_css = _css_rule(self.css, ".yunos-shell-omnibox-anchor")
        form_css = _css_rule(self.css, ".yunos-home-omnibox")
        self.assertRegex(
            self.css,
            r"--yunos-shell-omnibox-width\s*:\s*clamp\([^;]+\)",
        )
        self.assertRegex(
            self.css,
            r"--yunos-shell-omnibox-height\s*:\s*clamp\([^;]+\)",
        )
        self.assertIn("var(--yunos-shell-omnibox-width)", anchor_css)
        self.assertIn("var(--yunos-shell-omnibox-height)", anchor_css)
        self.assertRegex(anchor_css, r"border-radius\s*:\s*(?:999px|50%)")
        self.assertRegex(anchor_css, r"width\s*:\s*min\(")
        self.assertRegex(form_css, r"border-radius\s*:\s*(?:999px|50%)")
        self.assertRegex(form_css, r"overflow\s*:\s*hidden")

    def test_mode_switch_is_embedded_and_exposes_both_explicit_modes(self) -> None:
        self.assertIn("记忆搜索", self.home)
        self.assertRegex(self.home, r">\s*AI\s*<")
        self.assertRegex(self.home, r"data-omnibox-mode=['\"]search['\"]")
        self.assertRegex(self.home, r"data-omnibox-mode=['\"]ai['\"]")
        self.assertLess(
            self.home.index('id="home-omnibox-input"'),
            self.home.index('class="yunos-home-omnibox-modes"'),
        )
        self.assertRegex(
            _css_rule(self.css, ".yunos-home-omnibox"),
            r"display\s*:\s*flex",
        )
        interactive_tags = re.findall(
            r"<(?:button|input)\b[^>]*>", self.home, flags=re.DOTALL
        )
        self.assertGreaterEqual(len(interactive_tags), 3)
        self.assertRegex(
            self.home,
            r"(?:role=['\"](?:group|radiogroup|tablist)['\"]|"
            r"aria-label=['\"][^'\"]*(?:模式|mode)[^'\"]*['\"])",
        )
        self.assertRegex(
            self.css,
            r"(?is)(?:omnibox[^,{]*mode|mode[^,{]*omnibox)[^{]*\{[^}]*"
            r"(?:display\s*:\s*(?:flex|inline-flex|grid)|position\s*:\s*absolute)",
        )

    def test_placeholders_cover_search_and_ai_without_auto_intent(self) -> None:
        combined = self.home + "\n" + self.javascript
        self.assertIn("搜索记忆", combined)
        self.assertIn("向 YunOS 提问", combined)
        self.assertIn("HOME_OMNIBOX_PLACEHOLDERS", self.javascript)
        self.assertRegex(self.javascript, r"homeOmniboxInput\.placeholder\s*=")
        self.assertNotRegex(
            self.javascript,
            r"(?i)(?:detect|infer|guess|auto)[A-Za-z_$]*Intent",
        )

    def test_mode_storage_is_yunos_namespaced_validated_and_failure_safe(self) -> None:
        self.assertRegex(
            self.javascript,
            r"(['\"])yunos\.homeOmnibox\.mode\1",
        )
        self.assertIn("localStorage.getItem", self.javascript)
        self.assertIn("localStorage.setItem", self.javascript)
        for method in ("getItem", "setItem"):
            with self.subTest(storage_method=method):
                self.assertRegex(
                    self.javascript,
                    rf"(?s)try\s*\{{.{{0,600}}?localStorage\.{method}"
                    r".{0,600}?\}\s*catch\s*(?:\([^)]*\))?\s*\{",
                )
        self.assertRegex(
            self.javascript,
            r"new Set\(\s*\[\s*['\"]search['\"]\s*,\s*['\"]ai['\"]\s*\]\s*\)",
        )
        self.assertRegex(self.javascript, r"HOME_OMNIBOX_MODES\.has\(")
        self.assertRegex(
            self.javascript,
            r"(?is)(?:getItem|read|load|restore)[^;]{0,500}"
            r"(?:set|apply|render|mode|search)",
        )
        self.assertRegex(
            self.javascript,
            r"HOME_OMNIBOX_DEFAULT_MODE\s*=\s*['\"]search['\"]",
        )
        self.assertIn(
            "setHomeOmniboxMode(readHomeOmniboxMode())",
            self.javascript,
        )

    def test_submit_dispatches_only_a_frontend_custom_event(self) -> None:
        submit_bodies = _listener_bodies(self.javascript, "submit")
        frontend_submitters = [
            body
            for body in submit_bodies
            if "CustomEvent" in body and "dispatchEvent" in body
        ]
        self.assertTrue(frontend_submitters, "Omnibox form must dispatch a CustomEvent")
        submit_body = frontend_submitters[0]
        self.assertIn("preventDefault", submit_body)
        self.assertRegex(submit_body, r"(['\"])yunos:home-omnibox-submit\1")
        self.assertRegex(submit_body, r"\bdetail\s*:")
        self.assertRegex(submit_body, r"(?i)\bquery\b")
        self.assertRegex(submit_body, r"(?i)\bmode\b")
        self.assertRegex(submit_body, r"\bbubbles\s*:\s*true")
        self.assertRegex(submit_body, r"\bcancelable\s*:\s*true")
        self.assertNotRegex(
            submit_body,
            r"(?i)\b(?:fetch|XMLHttpRequest|sendBeacon)\b|/api/|https?://",
        )

    def test_escape_blurs_the_current_control_without_discarding_input(self) -> None:
        escape_bodies = [
            body
            for body in _listener_bodies(self.javascript, "keydown")
            if re.search(r"key\s*[!=]={1,2}\s*['\"]Escape['\"]", body)
        ]
        omnibox_escape = [
            body
            for body in escape_bodies
            if ".blur(" in body
            and re.search(
                r"(?:homeOmniboxInput|home-omnibox-input|currentTarget|activeElement)",
                body,
            )
        ]
        self.assertTrue(
            omnibox_escape,
            "Escape must blur the focused Omnibox",
        )
        self.assertNotRegex(omnibox_escape[0], r"\.value\s*=\s*['\"]['\"]")

    def test_native_tab_order_aria_and_visible_focus_are_preserved(self) -> None:
        input_tag = re.search(r"<input\b[^>]*>", self.home, flags=re.DOTALL).group(0)
        input_attributes = _attributes(input_tag)
        self.assertTrue(
            input_attributes.get("aria-label")
            or 'for="home-omnibox-input"' in self.home,
            "Omnibox input needs an accessible name",
        )
        self.assertIn('data-has-value="false"', self.home)
        self.assertIn("syncHomeOmniboxValueState", self.javascript)
        self.assertIn('[data-has-value="true"]', self.css)
        self.assertNotRegex(self.home, r"tabindex\s*=\s*['\"]-[1-9]")
        mode_buttons = re.findall(r"<button\b[^>]*>", self.home, flags=re.DOTALL)
        self.assertGreaterEqual(len(mode_buttons), 2)
        for button_tag in mode_buttons:
            attributes = _attributes(button_tag)
            self.assertEqual(attributes.get("type"), "button")
            self.assertIn("aria-pressed", attributes)
        self.assertRegex(
            self.css,
            r"(?is)(?:omnibox[^,{]*:(?:focus-within|focus-visible)|"
            r"[^,{]*:(?:focus-within|focus-visible)[^,{]*omnibox)[^{]*\{[^}]*"
            r"(?:outline|box-shadow|border-color)",
        )

    def test_home_does_not_gain_deferred_or_chat_style_features(self) -> None:
        for forbidden in (
            "今天想聊点什么",
            "有什么可以帮你",
            "欢迎",
            "推荐问题",
            "快捷按钮",
            "上传",
            "语音",
            "麦克风",
            "自动模式",
            "自动意图",
        ):
            self.assertNotIn(forbidden, self.home)
        self.assertNotRegex(self.home, r"<(?:textarea|input)[^>]+type=['\"]file['\"]")

    def test_home_overlay_shell_keeps_explicit_omnibox_anchor_position(self) -> None:
        self.assertIn('data-page-state="home"', self.html)
        self.assertIn('data-sidebar-mode="overlay"', self.html)
        self.assertIn(
            'desktopShell.dataset.sidebarMode = pageState === "home" ? "overlay" : "push"',
            self.javascript,
        )
        anchor_css = _css_rule(self.css, ".yunos-shell-omnibox-anchor")
        for declaration in (
            "top: 41.5%",
            "left: 50%",
            "transform: translate(-50%, -50%)",
        ):
            self.assertIn(declaration, anchor_css)
        self.assertNotRegex(
            self.css,
            r"(?is)data-sidebar-(?:mode|open)[^{,]*\.yunos-shell-omnibox-anchor",
        )


if __name__ == "__main__":
    unittest.main()
