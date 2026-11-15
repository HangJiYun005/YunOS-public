from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
I18N = WEB / "i18n"
EN = I18N / "en.js"
ZH_CN = I18N / "zh-CN.js"
EN_EXTRAS = (
    I18N / "en-shell-mobile-extra.js",
    I18N / "en-legacy-core.js",
    I18N / "en-legacy-tools.js",
    I18N / "en-operational-extra.js",
)
ZH_CN_EXTRAS = (
    I18N / "zh-CN-shell-mobile-extra.js",
    I18N / "zh-CN-legacy-core.js",
    I18N / "zh-CN-legacy-tools.js",
    I18N / "zh-CN-operational-extra.js",
)
I18N_INDEX = I18N / "index.js"
INDEX = WEB / "index.html"
APP = WEB / "app.js"
MANIFEST = WEB / "manifest.webmanifest"
SERVICE_WORKER = WEB / "service-worker.js"

I18N_FILES = (EN, ZH_CN, *EN_EXTRAS, *ZH_CN_EXTRAS, I18N_INDEX)
CATALOG_KEY = re.compile(
    r"""(?m)^\s*["']([A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+)["']\s*:"""
)

CRITICAL_ZH = {
    "common.back": "返回",
    "common.close": "关闭",
    "common.retry": "重试",
    "common.loading": "正在加载",
    "nav.now": "当前",
    "nav.projects": "项目",
    "nav.waiting": "等待",
    "nav.decisions": "决策",
    "nav.delegations": "委托",
    "nav.search": "搜索",
    "nav.ai": "AI",
    "operational.currentPhase": "当前阶段",
    "operational.authorityBaseline": "权威基线",
    "operational.nextApproved": "下一批准步骤",
    "status.authorized": "已授权",
    "status.running": "执行中",
    "delegation.overreachDetected": "检测到越权",
    "offline.cachedContent": "已缓存内容",
    "offline.cachedAt": "缓存于 {time}",
    "status.active": "进行中",
    "status.paused": "已暂停",
    "status.completed": "已完成",
    "freshness.stale": "状态陈旧",
    "freshness.fresh": "状态新鲜",
}

NODE_BOOTSTRAP = r"""
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = process.cwd();
const sandbox = {
  console,
  Intl,
  Date,
  URL,
  URLSearchParams,
  TextEncoder,
  TextDecoder,
  setTimeout,
  clearTimeout,
  navigator: {
    language: "en-US",
    languages: ["en-US", "en"],
  },
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

for (const relativePath of [
  "web/i18n/en.js",
  "web/i18n/zh-CN.js",
  "web/i18n/en-shell-mobile-extra.js",
  "web/i18n/zh-CN-shell-mobile-extra.js",
  "web/i18n/en-legacy-core.js",
  "web/i18n/zh-CN-legacy-core.js",
  "web/i18n/en-legacy-tools.js",
  "web/i18n/zh-CN-legacy-tools.js",
  "web/i18n/en-operational-extra.js",
  "web/i18n/zh-CN-operational-extra.js",
  "web/i18n/index.js",
]) {
  const absolutePath = path.join(root, relativePath);
  const source = fs.readFileSync(absolutePath, "utf8");
  vm.runInContext(source, sandbox, { filename: relativePath });
}

const api = sandbox.YunOSI18n || sandbox.window.YunOSI18n;
assert(api && typeof api === "object", "window.YunOSI18n must be exposed");
assert.strictEqual(typeof api.t, "function", "YunOSI18n.t must be a function");

const catalogs = api.catalogs || (api.__test && api.__test.catalogs);
assert(catalogs && catalogs.en && catalogs["zh-CN"],
  "catalogs must be exposed through YunOSI18n.catalogs or YunOSI18n.__test.catalogs");

"""


def catalog_keys(source: str) -> list[str]:
    return CATALOG_KEY.findall(source)


class V901LocalizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = {
            path: path.read_text(encoding="utf-8") if path.is_file() else ""
            for path in (*I18N_FILES, INDEX, APP, SERVICE_WORKER)
        }
        cls.manifest = (
            json.loads(MANIFEST.read_text(encoding="utf-8"))
            if MANIFEST.is_file()
            else {}
        )

    def require_i18n_files(self) -> None:
        missing = [str(path.relative_to(ROOT)) for path in I18N_FILES if not path.is_file()]
        self.assertFalse(
            missing,
            "missing localization files: " + ", ".join(missing),
        )

    def run_node(self, case_source: str) -> str:
        self.require_i18n_files()
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the dependency-free VM tests")
        completed = subprocess.run(
            [node, "-e", NODE_BOOTSTRAP + "\n" + textwrap.dedent(case_source)],
            cwd=ROOT,
            env={**os.environ, "TZ": "Asia/Shanghai"},
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            self.fail(
                "Node VM localization assertion failed:\n"
                + (completed.stderr or completed.stdout)
            )
        return completed.stdout

    def test_expected_localization_files_and_global_api_exist(self) -> None:
        self.require_i18n_files()
        self.run_node(
            """
            for (const name of [
              "t",
              "enumLabel",
              "formatFullDate",
              "formatShortDate",
              "formatDateTime",
              "formatRelativeTime",
              "formatNumber",
              "formatCount",
              "formatFileSize",
              "formatDuration",
            ]) {
              assert.strictEqual(typeof api[name], "function",
                `YunOSI18n.${name} must be a function`);
            }
            """
        )

    def test_catalog_keys_are_unique_identical_and_sufficient(self) -> None:
        self.require_i18n_files()
        en_keys = [
            key
            for path in (EN, *EN_EXTRAS)
            for key in catalog_keys(self.sources[path])
        ]
        zh_keys = [
            key
            for path in (ZH_CN, *ZH_CN_EXTRAS)
            for key in catalog_keys(self.sources[path])
        ]
        for path, keys in (
            (Path("combined English catalogs"), en_keys),
            (Path("combined Simplified Chinese catalogs"), zh_keys),
        ):
            duplicates = sorted(
                key for key, count in Counter(keys).items() if count > 1
            )
            self.assertFalse(
                duplicates,
                f"{path} contains duplicate translation keys: "
                + ", ".join(duplicates),
            )
            self.assertGreaterEqual(
                len(keys),
                220,
                f"{path} must contain at least 220 explicit keys",
            )
        self.assertEqual(
            set(en_keys),
            set(zh_keys),
            "English and Simplified Chinese catalogs must have exactly the same keys",
        )
        self.assertEqual(
            len(en_keys),
            len(set(en_keys)),
            "English catalog source must not overwrite a duplicate key",
        )
        self.assertEqual(
            len(zh_keys),
            len(set(zh_keys)),
            "Chinese catalog source must not overwrite a duplicate key",
        )

    def test_default_locale_is_zh_cn_with_english_fallback(self) -> None:
        self.run_node(
            """
            const locale = typeof api.getLocale === "function"
              ? api.getLocale()
              : (api.locale || api.defaultLocale);
            const fallbackLocale = typeof api.getFallbackLocale === "function"
              ? api.getFallbackLocale()
              : (api.fallbackLocale || api.FALLBACK_LOCALE);
            assert.strictEqual(locale, "zh-CN",
              "English browser preferences must not override the zh-CN default");
            assert.strictEqual(fallbackLocale, "en");
            assert.strictEqual(api.t("nav.now"), "当前");
            """
        )

    def test_critical_chinese_terms_and_catalog_quality(self) -> None:
        expected = json.dumps(CRITICAL_ZH, ensure_ascii=False)
        self.run_node(
            f"""
            const expected = {expected};
            for (const [key, value] of Object.entries(expected)) {{
              assert.strictEqual(catalogs["zh-CN"][key], value, key);
              if (!value.includes("{{")) {{
                assert.strictEqual(api.t(key), value, key);
              }}
              assert.strictEqual(typeof catalogs.en[key], "string", key);
              assert(catalogs.en[key].trim().length > 0, key);
            }}
            const chineseValues = Object.values(catalogs["zh-CN"])
              .filter((value) => /[\\u3400-\\u9fff]/u.test(String(value)));
            assert(chineseValues.length >= 160,
              "the zh-CN catalog must contain substantial real Chinese coverage");
            """
        )

    def test_missing_chinese_key_falls_back_to_english(self) -> None:
        self.run_node(
            """
            const en = { ...catalogs.en };
            const zh = { ...catalogs["zh-CN"] };
            delete zh["common.retry"];
            const isolated = {
              console,
              Intl,
              Date,
              URL,
              URLSearchParams,
              TextEncoder,
              TextDecoder,
              setTimeout,
              clearTimeout,
              navigator: { language: "en-US", languages: ["en-US", "en"] },
              YunOSI18nCatalogEn: Object.freeze(en),
              YunOSI18nCatalogZhCN: Object.freeze(zh),
            };
            isolated.window = isolated;
            isolated.self = isolated;
            isolated.globalThis = isolated;
            vm.createContext(isolated);
            vm.runInContext(
              fs.readFileSync(path.join(root, "web/i18n/index.js"), "utf8"),
              isolated,
              { filename: "web/i18n/index.js" },
            );
            assert.strictEqual(
              isolated.YunOSI18n.t("common.retry"),
              en["common.retry"],
            );
            assert.strictEqual(catalogs["zh-CN"]["common.retry"], "重试",
              "isolated fallback testing must not mutate the production catalog");
            """
        )

    def test_double_missing_key_uses_safe_original_text(self) -> None:
        self.run_node(
            """
            const original = "Provider status: PROVIDER_INTERNAL_V2";
            const rendered = api.t(
              "missing.provider.status",
              {},
              original,
            );
            assert.strictEqual(rendered, original);
            assert(!rendered.includes("undefined"));
            assert(rendered.length > 0);
            """
        )

    def test_interpolation_missing_params_and_xss_are_plain_text(self) -> None:
        self.run_node(
            """
            assert(catalogs.en["search.moreMatches"],
              "search.moreMatches must be a parameterized English baseline key");
            assert(catalogs["zh-CN"]["search.moreMatches"],
              "search.moreMatches must exist in zh-CN");

            const missing = api.t("search.moreMatches", {});
            assert.strictEqual(typeof missing, "string");
            assert(!missing.includes("undefined"),
              "missing interpolation parameters must never render undefined");

            sandbox.__xssExecuted = false;
            const payload =
              '<img src=x onerror="window.__xssExecuted=true"><script>window.__xssExecuted=true</script>';
            const rendered = api.t("search.moreMatches", { count: payload });
            assert.strictEqual(typeof rendered, "string");
            assert.strictEqual(sandbox.__xssExecuted, false);
            assert(
              rendered.includes(payload) || rendered.includes("&lt;img"),
              "the payload must remain inert text, whether preserved or text-escaped",
            );
            """
        )
        source = self.sources[I18N_INDEX]
        for forbidden in ("eval(", "new Function(", ".innerHTML", "insertAdjacentHTML"):
            self.assertNotIn(
                forbidden,
                source,
                f"the localization layer must not use {forbidden}",
            )
        self.assertNotRegex(
            self.sources[APP],
            re.compile(r"innerHTML\s*=\s*`[^`]*\$\{\s*t\(", re.DOTALL),
            "translated text must not be interpolated into an HTML sink",
        )

    def test_enum_labels_are_display_only_and_unknown_values_are_preserved(self) -> None:
        self.run_node(
            """
            const record = {
              status: "active",
              freshness: "stale",
              provider: "PROVIDER_INTERNAL_V2",
            };
            const before = JSON.stringify(record);
            assert.strictEqual(api.enumLabel("status", record.status), "进行中");
            assert.strictEqual(
              api.enumLabel("freshness", record.freshness),
              "状态陈旧",
            );
            assert.strictEqual(
              api.enumLabel("provider", record.provider),
              "PROVIDER_INTERNAL_V2",
            );
            assert.strictEqual(JSON.stringify(record), before,
              "display mapping must not mutate API values");
            """
        )

    def test_user_generated_content_is_not_implicitly_translated(self) -> None:
        self.run_node(
            """
            const userContent = "用户创建的项目 Active / Completed";
            assert.strictEqual(
              api.t("user.generated.content", {}, userContent),
              userContent,
            );
            assert.strictEqual(api.t("Active", {}, "Active"), "Active");
            assert.strictEqual(api.enumLabel("status", "active"), "进行中",
              "only an explicit enum mapping may translate an internal value");
            """
        )

    def test_intl_date_relative_number_size_and_duration_formatters(self) -> None:
        self.run_node(
            """
            const value = "2026-07-31T08:30:00+08:00";
            const date = api.formatFullDate(value);
            const shortDate = api.formatShortDate(value);
            const dateTime = api.formatDateTime(value);
            assert(/2026年7月31日/.test(date), date);
            assert(/2026/.test(shortDate) && /07|7/.test(shortDate), shortDate);
            assert(/2026年7月31日/.test(dateTime), dateTime);
            assert(/08:30|8:30/.test(dateTime), dateTime);
            const base = Date.parse("2026-07-31T08:30:00+08:00");
            assert.strictEqual(
              api.formatRelativeTime(base - (3 * 60 * 1000), base),
              "3分钟前",
            );
            const originalRelativeTimeFormat = sandbox.Intl.RelativeTimeFormat;
            try {
              sandbox.Intl.RelativeTimeFormat = class {
                constructor() {
                  throw new Error("forced RelativeTimeFormat failure");
                }
              };
              assert.strictEqual(
                api.formatRelativeTime(base - (3 * 60 * 1000), base),
                "3分钟前",
              );
              assert.strictEqual(
                api.formatRelativeTime(base + (60 * 60 * 1000), base),
                "1小时后",
              );
            } finally {
              sandbox.Intl.RelativeTimeFormat = originalRelativeTimeFormat;
            }
            assert.strictEqual(api.formatNumber(1234567), "1,234,567");
            assert.strictEqual(api.formatCount(874, "files"), "874个文件");
            assert(/^540\\.7\\s*MB$/.test(api.formatFileSize(566965043)));
            const duration = api.formatDuration(2 * 60 * 60 * 1000);
            assert(/2/.test(duration) && /小时/.test(duration), duration);
            """
        )

    def test_html_language_title_script_order_and_i18n_attributes(self) -> None:
        html = self.sources[INDEX]
        self.assertRegex(html, r'<html\b[^>]*\blang=["\']zh-CN["\']')
        title = re.search(
            r"<title\b[^>]*>(.*?)</title>",
            html,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(title)
        self.assertIn("YunOS", title.group(1))
        self.assertRegex(
            title.group(1),
            r"[\u3400-\u9fff]",
            "the document title must identify the Chinese interface",
        )

        ordered_scripts = (
            "/i18n/en.js",
            "/i18n/zh-CN.js",
            "/i18n/en-shell-mobile-extra.js",
            "/i18n/zh-CN-shell-mobile-extra.js",
            "/i18n/en-legacy-core.js",
            "/i18n/zh-CN-legacy-core.js",
            "/i18n/en-legacy-tools.js",
            "/i18n/zh-CN-legacy-tools.js",
            "/i18n/en-operational-extra.js",
            "/i18n/zh-CN-operational-extra.js",
            "/i18n/index.js",
            "/app.js",
        )
        positions = [html.find(path) for path in ordered_scripts]
        self.assertTrue(
            all(position >= 0 for position in positions),
            "English, zh-CN, i18n runtime, and app scripts must all be loaded",
        )
        self.assertEqual(
            positions,
            sorted(positions),
            "catalogs and i18n runtime must load before app.js",
        )
        self.assertGreaterEqual(
            len(re.findall(r"\bdata-i18n=", html)),
            15,
            "static user-visible text needs auditable data-i18n coverage",
        )
        self.assertGreaterEqual(
            len(
                re.findall(
                    r"""\bdata-i18n-attr=["'][^"']*\baria-label:""",
                    html,
                )
            ),
            5,
            "ARIA labels need explicit localization hooks",
        )
        self.assertRegex(
            html,
            r"""\bdata-i18n-attr=["'][^"']*\b(?:placeholder|title):""",
            "placeholder or title localization hooks are required",
        )

    def test_manifest_keeps_yunos_name_and_has_chinese_description(self) -> None:
        self.assertTrue(MANIFEST.is_file())
        self.assertEqual(self.manifest.get("name"), "YunOS")
        self.assertEqual(self.manifest.get("short_name"), "YunOS")
        description = self.manifest.get("description", "")
        self.assertRegex(
            description,
            r"[\u3400-\u9fff]",
            "Manifest description must be Simplified Chinese",
        )
        if "lang" in self.manifest:
            self.assertEqual(self.manifest["lang"], "zh-CN")

    def test_service_worker_versions_and_precaches_localization_shell(self) -> None:
        source = self.sources[SERVICE_WORKER]
        version = re.search(
            r'APP_SHELL_VERSION\s*=\s*["\']([^"\']+)["\']',
            source,
        )
        self.assertIsNotNone(version)
        self.assertNotEqual(version.group(1), "v9-f-1")
        self.assertEqual("v10-e-agency-workspace-3", version.group(1))
        for path in (
            "/i18n/en.js",
            "/i18n/zh-CN.js",
            "/i18n/en-shell-mobile-extra.js",
            "/i18n/zh-CN-shell-mobile-extra.js",
            "/i18n/en-legacy-core.js",
            "/i18n/zh-CN-legacy-core.js",
            "/i18n/en-legacy-tools.js",
            "/i18n/zh-CN-legacy-tools.js",
            "/i18n/en-operational-extra.js",
            "/i18n/zh-CN-operational-extra.js",
            "/i18n/index.js",
        ):
            self.assertIn(path, source)

        self.assertIn('url.pathname.startsWith("/api/")', source)
        api_branch = source.split(
            'if (url.pathname.startsWith("/api/"))',
            1,
        )[1].split('if (request.mode === "navigate")', 1)[0]
        self.assertIn("event.respondWith(fetch(request))", api_branch)
        self.assertNotIn("cache", api_branch.lower())
        self.assertIn("APP_SHELL_CACHE_PREFIX", source)
        self.assertIn("cacheName.startsWith(APP_SHELL_CACHE_PREFIX)", source)

    def test_app_uses_translation_enum_and_formatter_api(self) -> None:
        source = self.sources[APP]
        self.assertRegex(
            source,
            r"(?:window|globalThis)\.YunOSI18n",
            "app.js must consume the browser-global YunOSI18n API",
        )
        for key in (
            "nav.now",
            "nav.projects",
            "nav.waiting",
            "nav.decisions",
            "nav.delegations",
        ):
            self.assertIn(key, source)
        translation_calls = re.findall(
            r"(?:\b|\.)t\s*\(",
            source,
        )
        self.assertGreaterEqual(
            len(translation_calls),
            30,
            "app.js must route dynamic user-visible text through t()",
        )
        self.assertRegex(source, r"\benumLabel\s*\(")
        formatter_uses = sum(
            bool(re.search(rf"\b{name}\s*\(", source))
            for name in (
                "formatDate",
                "formatDateTime",
                "formatRelativeTime",
                "formatNumber",
                "formatFileSize",
                "formatDuration",
            )
        )
        self.assertGreaterEqual(
            formatter_uses,
            4,
            "app.js must use the shared Intl-based formatters",
        )

    def test_reachable_ui_helpers_do_not_bypass_translation_layer(self) -> None:
        source = self.sources[APP]
        raw_text = (
            r'(?:"[^"\r\n]*[\u3400-\u9fff][^"\r\n]*"'
            r"|'[^'\r\n]*[\u3400-\u9fff][^'\r\n]*'"
            r"|`[^`]*[\u3400-\u9fff][^`]*`)"
        )
        patterns = {
            "setTitle": rf"\bsetTitle\(\s*{raw_text}",
            "renderEmpty": rf"\brenderEmpty\(\s*{raw_text}",
            "button": rf"\bbutton\(\s*{raw_text}",
            "metaItem": rf"\bmetaItem\(\s*{raw_text}",
            "placeholder assignment": rf"\.placeholder\s*=\s*{raw_text}",
            "title assignment": rf"\.title\s*=\s*{raw_text}",
            "textContent assignment": rf"\.textContent\s*=\s*{raw_text}",
            "localized aria/title attribute": (
                rf"\.setAttribute\(\s*[\"'](?:aria-label|title)[\"']\s*,\s*{raw_text}"
            ),
            "innerHTML user-visible text": rf"\.innerHTML\s*=\s*{raw_text}",
            "element text argument": (
                rf"\bel\(\s*[^,\r\n]+,\s*[^,\r\n]+,\s*{raw_text}"
            ),
        }
        findings = []
        for label, pattern in patterns.items():
            for match in re.finditer(pattern, source, re.DOTALL):
                line = source.count("\n", 0, match.start()) + 1
                excerpt = " ".join(match.group(0).split())[:180]
                findings.append(f"{label} at app.js:{line}: {excerpt}")
        self.assertFalse(
            findings,
            "reachable UI text bypasses t():\n" + "\n".join(findings),
        )

    def test_approved_english_allowlist_is_structured(self) -> None:
        allowlist_files = tuple(
            path
            for path in (
                I18N / "allowlist.js",
                I18N / "allowlist.json",
            )
            if path.is_file()
        )
        if allowlist_files:
            source = allowlist_files[0].read_text(encoding="utf-8")
            for token in ("brand", "technical", "user"):
                self.assertIn(token, source.lower())
            for token in ("YunOS", "API", "Runtime", "Provider"):
                self.assertIn(token, source)
            return

        self.run_node(
            """
            const allowlist =
              api.allowedEnglish || (api.__test && api.__test.allowedEnglish);
            assert(allowlist && typeof allowlist === "object",
              "expose a structured allowedEnglish catalog or add allowlist.js/json");
            const keys = Object.keys(allowlist).map((key) => key.toLowerCase());
            assert(keys.some((key) => key.includes("brand")));
            assert(keys.some((key) => key.includes("technical")));
            assert(keys.some((key) => key.includes("user")));
            const serialized = JSON.stringify(allowlist);
            for (const token of [
              "YunOS", "ChatGPT", "Claude", "Gemini", "Ollama",
              "API", "Runtime", "Provider", "Markdown", "TXT", "Enter",
              "memory_summaries", "mark/tag", "real",
            ]) {
              assert(serialized.includes(token), token);
            }
            """
        )


if __name__ == "__main__":
    unittest.main()
