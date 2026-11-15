import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "index.html"
STYLE = ROOT / "web" / "style.css"
APP = ROOT / "web" / "app.js"
BROWSER_FILES = (
    ROOT / "tests" / "browser" / "v81_ai_document_result.js",
    ROOT / "tests" / "browser" / "v81_ai_document_execute.js",
    ROOT / "tests" / "browser" / "v81_ai_document_recover_formal.js",
    ROOT / "tests" / "browser" / "v81_ai_document_traceability.js",
    ROOT / "tests" / "browser" / "v81_ai_document_simulations.js",
    ROOT / "tests" / "browser" / "v81_ai_document_no_authority.js",
    ROOT / "tests" / "browser" / "v81_ai_document_responsive.js",
)


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


class V81AiDocumentResultContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.css = STYLE.read_text(encoding="utf-8")
        cls.javascript = APP.read_text(encoding="utf-8")
        cls.browser = "\n".join(path.read_text(encoding="utf-8") for path in BROWSER_FILES)
        cls.ai_javascript = cls.javascript.split("function homeAiIsWorking", 1)[1].split(
            "function returnToHomeMemorySearch", 1
        )[0]
        cls.ai_css = cls.css.split(
            '.yunos-desktop-shell[data-page-state="ai"] .yunos-shell-home-region', 1
        )[1].split(".nav {", 1)[0]

    def test_home_contains_one_accessible_document_mount_after_omnibox(self) -> None:
        self.assertEqual(1, self.html.count('id="home-ai-document"'))
        self.assertIn('id="home-ai-document-status"', self.html)
        self.assertIn('id="home-ai-document-content"', self.html)
        self.assertIn('aria-label="YunOS AI 文档结果"', self.html)
        status_markup = self.html.split('id="home-ai-document-status"', 1)[1].split(">", 1)[0]
        self.assertIn('aria-live="polite"', status_markup)
        document_markup = self.html.split('id="home-ai-document"', 1)[1].split(">", 1)[0]
        self.assertNotIn('aria-live=', document_markup)
        self.assertLess(
            self.html.index('id="home-omnibox-form"'),
            self.html.index('id="home-ai-document"'),
        )

    def test_state_machine_covers_required_terminal_and_transient_states(self) -> None:
        for phase in (
            "idle",
            "preparing-preview",
            "preview-ready",
            "executing",
            "receiving-result",
            "success",
            "truncated",
            "insufficient-evidence",
            "provider-error",
            "cancelled-or-unavailable",
        ):
            with self.subTest(phase=phase):
                self.assertIn(f'"{phase}"', self.javascript)
        phase_setter = _function_body(self.javascript, "setHomeAiPhase")
        self.assertIn('dataset.aiState = nextPhase', phase_setter)
        self.assertIn('setAttribute("aria-busy"', phase_setter)

    def test_ai_shell_reuses_home_region_with_push_sidebar(self) -> None:
        shell = _function_body(self.javascript, "setShellPageState")
        self.assertIn('nextState === "ai"', shell)
        self.assertIn('pageState === "workspace" ? "workspace" : "home"', shell)
        self.assertIn('pageState === "home" ? "overlay" : "push"', shell)
        self.assertIn('desktopShell.dataset.sidebarMode = pageState === "home" ? "overlay" : "push"', shell)
        self.assertIn("shellSidebarState.hover = false", shell)
        self.assertIn("syncShellSidebar()", shell)
        self.assertIn('[data-page-state="ai"] [data-shell-region="workspace"]', self.css)

    def test_search_and_ai_submission_paths_remain_separate(self) -> None:
        search = _function_body(self.javascript, "handleHomeMemorySearchSubmit")
        ai = _function_body(self.javascript, "handleHomeAiSubmit")
        self.assertIn('mode !== "search"', search)
        self.assertIn('/api/search?', search)
        self.assertNotIn("runtime-preview", search)
        self.assertIn('mode !== "ai"', ai)
        self.assertIn("startHomeAiPreview(question)", ai)
        self.assertIn("homeAiState.phase !== HOME_AI_PHASES.IDLE", ai)
        self.assertNotIn('/api/search', ai)

    def test_preview_is_review_only_and_never_calls_execute(self) -> None:
        preview = _function_body(self.javascript, "startHomeAiPreview")
        self.assertIn('/api/runtime-preview?', preview)
        self.assertNotIn('/api/runtime-execute', preview)
        self.assertNotIn('method: "POST"', preview)
        self.assertIn('validateHomeAiPreviewIdentity(preview)', preview)
        self.assertIn('provider call=0', preview)

    def test_preview_summary_exposes_the_required_execution_facts(self) -> None:
        summary = _function_body(self.javascript, "appendHomeAiExecutionSummary")
        for label in ("记忆范围", "证据", "已批准画像", "模型", "输入 token 估算", "外部 API"):
            with self.subTest(label=label):
                self.assertIn(f'"{label}"', summary)
        preview = _function_body(self.javascript, "renderHomeAiPreview")
        self.assertIn('id = "home-ai-execute"', preview)
        self.assertIn("executionAvailable", preview)
        self.assertIn("runtime_sidecar", preview)
        self.assertIn("state.canExecute", preview)

    def test_execute_locks_before_the_only_post_and_sends_reviewed_binding(self) -> None:
        execute = _function_body(self.javascript, "executeHomeAiDocumentOnce")
        self.assertEqual(1, execute.count('api("/api/runtime-execute"'))
        self.assertLess(
            execute.index("homeAiState.executionAttempted = true"),
            execute.index('api("/api/runtime-execute"'),
        )
        self.assertIn("homeAiState.inFlight", execute)
        self.assertIn("homeAiState.phase !== HOME_AI_PHASES.PREVIEW_READY", execute)
        self.assertIn("expected_bindings: homeAiState.preview.reviewed_execution_binding", execute)
        self.assertIn("confirmed: true", execute)
        self.assertIn("validateHomeAiExecutionEnvelope", execute)
        self.assertNotIn("max_tokens", execute)

    def test_terminal_rendering_preserves_success_truncation_and_error_truth(self) -> None:
        result = _function_body(self.javascript, "renderHomeAiResult")
        self.assertIn('finish_reason === "length"', result)
        self.assertIn("HOME_AI_PHASES.TRUNCATED", result)
        self.assertIn("HOME_AI_PHASES.SUCCESS", result)
        self.assertIn("HOME_AI_PHASES.PROVIDER_ERROR", result)
        self.assertIn("回答可能被截断", result)
        self.assertIn("不会自动续写或自动重试", result)
        self.assertIn('"ai.executionFailed"', result)

    def test_refresh_recovery_uses_ledger_and_fails_closed_on_identity_drift(self) -> None:
        restore = _function_body(self.javascript, "restoreHomeAiSession")
        validate = _function_body(self.javascript, "validateHomeAiRecoveredResult")
        self.assertIn('/api/runtime-run?model_run_id=', restore)
        self.assertIn('/api/runtime-preview?', restore)
        self.assertLess(restore.index('/api/runtime-run?'), restore.index('/api/runtime-preview?'))
        self.assertIn("validateHomeAiRecoveredResult", restore)
        self.assertIn('"error.safeRestoreSummary"', restore)
        for binding in (
            "artifact_identity",
            "wire_body_sha256",
            "delivery_identity",
            "assembly_identity",
            "model_context_sha256",
            "profile_item_ids",
            "evidence_reference_ids",
        ):
            with self.subTest(binding=binding):
                self.assertIn(binding, validate)

    def test_sources_reuse_conversation_detail_and_message_anchor(self) -> None:
        opener = _function_body(self.javascript, "openHomeAiSource")
        loader = _function_body(self.javascript, "loadConversation")
        conversation = _function_body(self.javascript, "renderConversation")
        self.assertIn('setShellPageState("workspace")', opener)
        self.assertIn("focusMessageId: anchor.message_id", opener)
        self.assertIn("fromAiDocument: true", opener)
        self.assertIn("fromAiDocument: Boolean(options.fromAiDocument)", loader)
        self.assertIn("返回 AI 结果", conversation)
        self.assertIn("returnToHomeAiDocument", conversation)

    def test_visual_contract_is_reading_width_document_not_chat_ui(self) -> None:
        for selector in (
            ".yunos-home-ai-document",
            ".yunos-ai-document-body",
            ".yunos-ai-answer",
            ".yunos-ai-supporting-sections",
            ".yunos-ai-disclosure",
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, self.ai_css)
        self.assertIn("overflow-y: auto", self.ai_css)
        self.assertIn("width: min(820px, 100%)", self.ai_css)
        self.assertNotIn("linear-gradient", self.ai_css)
        self.assertNotRegex(self.ai_javascript.lower(), r"\b(chat-bubble|avatar|assistant-avatar)\b")

    def test_frontend_does_not_create_provider_or_secret_surface(self) -> None:
        for forbidden in (
            "DEEPSEEK_API_KEY",
            "process.env",
            "credential_path",
            "/api/chat",
            "/api/agent",
            "Authorization",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.ai_javascript)

    def test_browser_acceptance_covers_exactly_once_refresh_states_and_viewports(self) -> None:
        for token in (
            "/api/runtime-preview",
            "/api/runtime-execute",
            "/api/runtime-run",
            "preview-ready",
            "receiving-result",
            "simulated-truncated",
            "simulated-provider-error",
            "no-authority state emitted an execute POST",
            "exactly one",
            "2560, height: 1440",
            "1920, height: 1080",
            "1707, height: 1067",
            "1440, height: 900",
            "1280, height: 720",
            "1100, height: 800",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.browser)


if __name__ == "__main__":
    unittest.main()
