import json
import re
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
OFFLINE_SHELL = (WEB / "offline-shell.html").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")
STYLE = (WEB / "style.css").read_text(encoding="utf-8")
SERVICE_WORKER = (WEB / "service-worker.js").read_text(encoding="utf-8")
LOCAL_WEB = (ROOT / "tools" / "local_web.py").read_text(encoding="utf-8")
MANIFEST = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
ZH_EXTRA = (WEB / "i18n" / "zh-CN-shell-mobile-extra.js").read_text(
    encoding="utf-8"
)
EN_EXTRA = (WEB / "i18n" / "en-shell-mobile-extra.js").read_text(
    encoding="utf-8"
)


def function_body(source: str, function_name: str) -> str:
    marker = f"function {function_name}("
    start = source.index(marker)
    open_paren = source.index("(", start)
    depth = 0
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
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
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


class V10EAgencyWorkspacePwaContractTest(unittest.TestCase):
    def test_disposable_browser_fixture_script_is_directly_runnable(self) -> None:
        script = ROOT / "tests" / "browser" / "prepare_v10_e_cognitive_fixture.py"
        with TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            database = Path(temporary) / "operational-state.db"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(script),
                    "--output",
                    str(database),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=90,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            metadata = json.loads(completed.stdout)
            self.assertEqual("waiting_acceptance", metadata["status"])
            self.assertEqual("act", metadata["agency_level"])
            self.assertTrue(database.is_file())
            with sqlite3.connect(
                f"file:{database.as_posix()}?mode=ro",
                uri=True,
            ) as connection:
                self.assertEqual(
                    "ok",
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                )
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM cognitive_runs WHERE run_id=?",
                        (metadata["run_id"],),
                    ).fetchone()[0],
                )

    def test_manifest_keeps_single_minimal_yunos_home(self) -> None:
        self.assertEqual("/", MANIFEST["start_url"])
        self.assertEqual("/", MANIFEST["scope"])
        self.assertEqual("standalone", MANIFEST["display"])
        self.assertEqual("portrait-primary", MANIFEST["orientation"])
        self.assertIn("YunOS", MANIFEST["name"])
        self.assertRegex(MANIFEST["description"], r"[\u4e00-\u9fff]")
        self.assertNotIn("shortcuts", MANIFEST)

    def test_service_worker_never_caches_private_or_cognitive_api(self) -> None:
        api_branch = SERVICE_WORKER.split(
            'if (url.pathname.startsWith("/api/"))', 1
        )[1].split('if (request.mode === "navigate")', 1)[0]
        self.assertIn("event.respondWith(fetch(request))", api_branch)
        for forbidden in ("cache", "fallback", "offline", "clone"):
            self.assertNotIn(forbidden, api_branch.lower())
        precache = SERVICE_WORKER.split(
            "const APP_SHELL_URLS = Object.freeze([", 1
        )[1].split("]);", 1)[0]
        self.assertNotRegex(precache, re.compile(r"/api/", re.IGNORECASE))
        self.assertNotIn('addEventListener("sync"', SERVICE_WORKER)
        self.assertNotIn('addEventListener("periodicsync"', SERVICE_WORKER.lower())

    def test_app_shell_revision_and_references_are_consistent(self) -> None:
        version_match = re.search(
            r'APP_SHELL_VERSION\s*=\s*"([^"]+)"', SERVICE_WORKER
        )
        self.assertIsNotNone(version_match)
        version = version_match.group(1)
        self.assertEqual("v10-e-agency-workspace-3", version)
        precache = SERVICE_WORKER.split(
            "const APP_SHELL_URLS = Object.freeze([", 1
        )[1].split("]);", 1)[0]
        urls = re.findall(r'"([^"]+)"', precache)
        self.assertIn("/", urls)
        self.assertIn("/index.html", urls)
        self.assertIn(
            'const OFFLINE_SHELL_KEY = "/offline-shell.html"',
            SERVICE_WORKER,
        )
        self.assertIn("OFFLINE_SHELL_KEY", precache)
        for url in urls:
            path_only, _, query = url.partition("?")
            if query:
                self.assertRegex(query, r"^v=[a-z0-9-]+$", url)
            if path_only == "/":
                path_only = "/index.html"
            self.assertTrue((WEB / path_only.lstrip("/")).is_file(), url)
        for source_name, source in (
            ("index.html", INDEX),
            ("offline-shell.html", OFFLINE_SHELL),
        ):
            for referenced in re.findall(
                r'(?:src|href)="(/[^"]+\?v=([^"]+))"', source
            ):
                url, referenced_version = referenced
                self.assertTrue(referenced_version, f"{source_name}: {url}")
                self.assertIn(url, urls, f"{source_name}: {url}")

    def test_all_frontend_api_requests_are_http_no_store(self) -> None:
        api = function_body(APP, "api")
        self.assertIn('cache: "no-store"', api)
        sender = LOCAL_WEB.split("    def send_json", 1)[1].split(
            "    def send_error_json", 1
        )[0]
        self.assertIn('self.send_header("Cache-Control", "no-store")', sender)
        cognitive = LOCAL_WEB.split("    def api_cognitive_objectives", 1)[1].split(
            "    def api_operational_state", 1
        )[0]
        self.assertIn('response["cache_control"] = "no-store"', cognitive)
        self.assertIn('response["readonly"] = True', cognitive)

    def test_cognitive_web_surface_has_no_remote_mutation_route(self) -> None:
        get_handler = LOCAL_WEB.split("    def do_GET", 1)[1].split(
            "    def do_POST", 1
        )[0]
        post_handler = LOCAL_WEB.split("    def do_POST", 1)[1].split(
            "    def handle_api_get", 1
        )[0]
        self.assertIn('parsed.path.startswith("/api/cognitive/")', get_handler)
        self.assertNotIn("/api/cognitive/", post_handler)
        for method in ("do_PUT", "do_PATCH", "do_DELETE"):
            self.assertNotIn(f"def {method}", LOCAL_WEB)

    def test_workspace_exposes_document_and_supervision_contract(self) -> None:
        product = INDEX + APP
        self.assertIn("data-cognitive-workspace", product)
        self.assertTrue(
            "data-cognitive-supervision" in product
            or ".dataset.cognitiveSupervision" in product
        )
        for section in (
            "objective",
            "success",
            "prohibited",
            "context",
            "plan",
            "decisions",
            "execution",
            "verification",
            "outcome",
            "learning",
        ):
            self.assertTrue(
                f'data-cognitive-section="{section}"' in product
                or f'cognitiveSection("{section}")' in product,
                section,
            )
        for label in (
            "认知任务工作区",
            "目标理解",
            "成功标准",
            "明确不做",
            "使用的个人上下文",
            "计划",
            "决策点",
            "当前执行",
            "验证",
            "结果",
            "学习候选",
        ):
            self.assertIn(label, ZH_EXTRA)
        for enum_value in (
            "consult",
            "plan",
            "prepare",
            "act",
            "continue",
            "explore",
            "breakthrough",
        ):
            self.assertIn(enum_value, APP)

    def test_late_authoritative_workspace_replays_runtime_failure_truth(self) -> None:
        refresh = function_body(APP, "refreshHomeAiCognitiveWorkspace")
        unavailable = function_body(APP, "renderHomeAiUnavailable")
        hydrate = function_body(APP, "hydrateHomeAiCognitiveBundle")
        self.assertIn("HOME_AI_PHASES.CANCELLED_OR_UNAVAILABLE", refresh)
        self.assertIn("homeAiState.runtimeFailure", refresh)
        self.assertIn("technicalDetail: failure.technicalDetail", refresh)
        self.assertIn("executionUncertain: failure.executionUncertain", refresh)
        self.assertIn("homeAiState.runtimeFailure = {", unavailable)
        self.assertIn("refreshHomeAiCognitiveWorkspace()", hydrate)

    def test_mobile_workspace_is_explicitly_readonly_and_has_no_write_controls(self) -> None:
        product = INDEX + APP
        self.assertIn("data-cognitive-mobile-readonly", product)
        self.assertIn("只读", product)
        for forbidden_marker in (
            'data-cognitive-mutation',
            'data-cognitive-authorize',
            'data-cognitive-accept',
            'data-cognitive-write-tool',
            'data-cognitive-start-codex',
        ):
            self.assertNotIn(forbidden_marker, product)

    def test_workspace_has_responsive_overflow_guards_and_english_fallback(self) -> None:
        self.assertIn("data-cognitive-workspace", STYLE)
        cognitive_rules = "\n".join(
            block for block in STYLE.split("}")
            if "cognitive" in block.lower()
        )
        self.assertIn("min-width: 0", cognitive_rules)
        self.assertRegex(cognitive_rules, r"overflow-(?:x|wrap)")
        self.assertIn("function t(", APP)
        translate = function_body(APP, "t")
        self.assertIn("fallback", translate)
        for key in (
            "cognitive.workspaceTitle",
            "cognitive.section.objective",
            "cognitive.section.learning",
            "cognitive.supervisionTitle",
        ):
            self.assertIn(f'"{key}"', ZH_EXTRA)
            self.assertIn(f'"{key}"', EN_EXTRA)

    def test_workspace_truth_overlays_fail_visible_without_mutating_plan(self) -> None:
        refresh = function_body(APP, "refreshHomeAiCognitiveWorkspace")
        unavailable = function_body(APP, "renderHomeAiUnavailable")
        risk = function_body(APP, "cognitiveRiskLabel")
        plan_overlay = function_body(APP, "cognitivePlanDisplayState")
        stall = function_body(APP, "cognitiveStallStatus")

        self.assertIn("CANCELLED_OR_UNAVAILABLE", refresh)
        self.assertIn("runtimeFailure", refresh)
        self.assertIn("renderCognitiveWorkspace", refresh)
        self.assertIn("technicalDetail", unavailable)
        self.assertIn('"critical"', risk)
        self.assertIn('"cognitive.risk.unrecognized"', risk)
        self.assertNotIn('? value : "low"', risk)
        self.assertIn("checkpoint.completed_node_ids", plan_overlay)
        self.assertIn("checkpoint.current_node_ids", plan_overlay)
        self.assertNotIn("node.state =", plan_overlay)
        self.assertIn("COGNITIVE_STALL_THRESHOLD_MS", stall)
        self.assertIn("suspected_stall", stall)
        self.assertIn('"cognitive.stallOverdue"', stall)
        self.assertIn('"cognitive.stallUnknown"', stall)
        for key in (
            "cognitive.risk.critical",
            "cognitive.risk.unrecognized",
            "cognitive.stallSuspected",
            "cognitive.stallOverdue",
        ):
            self.assertIn(f'"{key}"', ZH_EXTRA)
            self.assertIn(f'"{key}"', EN_EXTRA)


if __name__ == "__main__":
    unittest.main()
