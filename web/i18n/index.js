(function initializeYunOSI18n(root) {
  "use strict";

  if (root.YunOSI18n) return;

  const DEFAULT_LOCALE = "zh-CN";
  const FALLBACK_LOCALE = "en";
  const EMPTY_CATALOG = Object.freeze({});
  function mergeCatalogs(...sources) {
    return Object.freeze(Object.assign(
      {},
      ...sources.filter((source) => source && typeof source === "object"),
    ));
  }

  const catalogs = Object.freeze({
    en: mergeCatalogs(
      root.YunOSI18nCatalogEn || EMPTY_CATALOG,
      root.YunOSI18nCatalogEnShellMobileExtra,
      root.YunOSI18nCatalogEnLegacyCore,
      root.YunOSI18nCatalogEnLegacyTools,
      root.YunOSI18nCatalogEnOperationalExtra,
    ),
    "zh-CN": mergeCatalogs(
      root.YunOSI18nCatalogZhCN || EMPTY_CATALOG,
      root.YunOSI18nCatalogZhCNShellMobileExtra,
      root.YunOSI18nCatalogZhCNLegacyCore,
      root.YunOSI18nCatalogZhCNLegacyTools,
      root.YunOSI18nCatalogZhCNOperationalExtra,
    ),
  });
  const supportedLocales = Object.freeze(["zh-CN", "en"]);
  const unsafeAttributePattern = /^(?:on|style$|srcdoc$|innerhtml$|outerhtml$|href$|src$|action$|formaction$|xlink:href$)/i;
  let currentLocale = DEFAULT_LOCALE;

  function hasOwn(target, key) {
    return Object.prototype.hasOwnProperty.call(target, key);
  }

  function normalizeLocale(locale) {
    const value = String(locale || "").trim().toLowerCase();
    if (value === "en" || value.startsWith("en-")) return "en";
    if (value === "zh" || value === "zh-cn" || value === "zh-hans" || value.startsWith("zh-hans-")) {
      return "zh-CN";
    }
    return "";
  }

  function safeKeyFallback(key) {
    return String(key || "").trim() ? "—" : "";
  }

  function templateFor(key, original) {
    const normalizedKey = String(key || "").trim();
    const searchOrder = currentLocale === FALLBACK_LOCALE
      ? [catalogs.en]
      : [catalogs["zh-CN"], catalogs.en];
    for (const catalog of searchOrder) {
      if (hasOwn(catalog, normalizedKey) && typeof catalog[normalizedKey] === "string") {
        return catalog[normalizedKey];
      }
    }
    const originalText = original === null || original === undefined ? "" : String(original);
    return originalText || safeKeyFallback(normalizedKey);
  }

  function interpolationValue(params, name) {
    if (!params || typeof params !== "object" || !hasOwn(params, name)) return "";
    const value = params[name];
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "bigint" || typeof value === "boolean") {
      return String(value);
    }
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }

  function t(key, params = {}, original = "") {
    const template = templateFor(key, original);
    return String(template).replace(/\{([A-Za-z0-9_.-]+)\}/g, (_match, name) => (
      interpolationValue(params, name)
    ));
  }

  function validDate(value) {
    if (value instanceof Date) {
      return Number.isNaN(value.getTime()) ? null : new Date(value.getTime());
    }
    if (value === null || value === undefined || value === "") return null;
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function formatDate(value, options, invalidKey = "error.invalidDate") {
    const date = validDate(value);
    if (!date) return t(invalidKey);
    try {
      return new Intl.DateTimeFormat(currentLocale, options).format(date);
    } catch {
      return date.toISOString();
    }
  }

  function formatFullDate(value) {
    return formatDate(value, {
      year: "numeric",
      month: "long",
      day: "numeric",
    });
  }

  function formatShortDate(value) {
    return formatDate(value, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
  }

  function formatDateTime(value, options = {}) {
    return formatDate(value, {
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      ...options,
    });
  }

  function formatRelativeTime(value, base = Date.now()) {
    const date = validDate(value);
    const baseDate = validDate(base);
    if (!date || !baseDate) return t("date.unknown");
    const deltaMs = date.getTime() - baseDate.getTime();
    const absoluteMs = Math.abs(deltaMs);
    const units = [
      ["year", 365 * 24 * 60 * 60 * 1000],
      ["month", 30 * 24 * 60 * 60 * 1000],
      ["week", 7 * 24 * 60 * 60 * 1000],
      ["day", 24 * 60 * 60 * 1000],
      ["hour", 60 * 60 * 1000],
      ["minute", 60 * 1000],
      ["second", 1000],
    ];
    const selected = units.find(([, milliseconds]) => absoluteMs >= milliseconds) || units.at(-1);
    const amount = Math.round(deltaMs / selected[1]);
    try {
      return new Intl.RelativeTimeFormat(currentLocale, { numeric: "auto" }).format(amount, selected[0]);
    } catch {
      const absoluteAmount = Math.abs(amount);
      const number = formatNumber(absoluteAmount);
      const plurality = absoluteAmount === 1 ? "one" : "other";
      const unit = t(
        `units.relative.${selected[0]}.${plurality}`,
        {},
        selected[0],
      );
      const key = amount < 0 ? "date.relativePast" : "date.relativeFuture";
      return t(key, { amount: number, unit }, `${number} ${unit}`);
    }
  }

  function formatNumber(value, options = {}) {
    const number = typeof value === "bigint" ? value : Number(value);
    if (typeof number !== "bigint" && !Number.isFinite(number)) return t("error.invalidNumber");
    try {
      return new Intl.NumberFormat(currentLocale, options).format(number);
    } catch {
      return String(number);
    }
  }

  function formatCount(value, unit = "items", options = {}) {
    const count = formatNumber(value, options);
    if (count === t("error.invalidNumber")) return count;
    const unitKey = String(unit || "items").includes(".") ? String(unit) : `units.${unit || "items"}`;
    return t(unitKey, { count }, `${count} ${String(unit || "items")}`);
  }

  function formatFileSize(value, options = {}) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes)) return t("error.invalidFileSize");
    const base = options.base === 1000 ? 1000 : 1024;
    const labels = base === 1000
      ? ["B", "KB", "MB", "GB", "TB", "PB"]
      : ["B", "KB", "MB", "GB", "TB", "PB"];
    const absoluteBytes = Math.abs(bytes);
    const exponent = absoluteBytes === 0
      ? 0
      : Math.min(labels.length - 1, Math.floor(Math.log(absoluteBytes) / Math.log(base)));
    const scaled = bytes / (base ** exponent);
    const maximumFractionDigits = Number.isInteger(options.maximumFractionDigits)
      ? Math.max(0, options.maximumFractionDigits)
      : (exponent === 0 ? 0 : 1);
    return `${formatNumber(scaled, {
      minimumFractionDigits: 0,
      maximumFractionDigits,
    })} ${labels[exponent]}`;
  }

  function formatDuration(value, options = {}) {
    const input = Number(value);
    if (!Number.isFinite(input)) return t("error.invalidDuration");
    const multiplier = options.inputUnit === "seconds" ? 1000 : 1;
    let remaining = Math.max(0, Math.round(Math.abs(input) * multiplier));
    const segments = [
      ["units.durationDay", 24 * 60 * 60 * 1000],
      ["units.durationHour", 60 * 60 * 1000],
      ["units.durationMinute", 60 * 1000],
      ["units.durationSecond", 1000],
    ];
    const parts = [];
    const maxParts = Number.isInteger(options.maxParts) ? Math.max(1, options.maxParts) : 2;
    for (const [key, size] of segments) {
      if (remaining < size && parts.length === 0 && key !== "units.durationSecond") continue;
      const count = Math.floor(remaining / size);
      if (count > 0 || (key === "units.durationSecond" && parts.length === 0)) {
        parts.push(t(key, { count: formatNumber(count) }));
        remaining -= count * size;
      }
      if (parts.length >= maxParts) break;
    }
    return `${input < 0 ? "-" : ""}${parts.join(currentLocale === "zh-CN" ? "" : " ")}`;
  }

  function enumVariants(value) {
    const raw = String(value === null || value === undefined ? "" : value).trim();
    const lower = raw.toLowerCase();
    const snake = lower.replace(/[\s-]+/g, "_");
    const camel = snake.replace(/_([a-z0-9])/g, (_match, character) => character.toUpperCase());
    return [...new Set([raw, lower, snake, camel].filter(Boolean))];
  }

  function translatedValue(key) {
    const localized = catalogs[currentLocale];
    if (hasOwn(localized, key)) return interpolateCatalogValue(localized[key]);
    if (hasOwn(catalogs.en, key)) return interpolateCatalogValue(catalogs.en[key]);
    return "";
  }

  function interpolateCatalogValue(value) {
    return typeof value === "string" ? value : "";
  }

  function enumLabel(group, value, context = "") {
    const normalizedGroup = String(group || "").trim();
    const normalizedContext = String(context || "").trim();
    if (!normalizedGroup) return String(value === null || value === undefined ? "" : value);
    for (const variant of enumVariants(value)) {
      const candidates = normalizedContext
        ? [
            `${normalizedGroup}.${normalizedContext}.${variant}`,
            `${normalizedContext}.${normalizedGroup}.${variant}`,
            `${normalizedGroup}.${variant}`,
          ]
        : [`${normalizedGroup}.${variant}`];
      for (const key of candidates) {
        const translated = translatedValue(key);
        if (translated) return translated;
      }
    }
    return String(value === null || value === undefined ? "" : value);
  }

  function elementParams(element) {
    const raw = element.getAttribute("data-i18n-params");
    if (!raw) return {};
    try {
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch {
      return {};
    }
  }

  function translatableNodes(rootNode, selector) {
    const nodes = [];
    if (rootNode && typeof rootNode.matches === "function" && rootNode.matches(selector)) {
      nodes.push(rootNode);
    }
    if (rootNode && typeof rootNode.querySelectorAll === "function") {
      nodes.push(...rootNode.querySelectorAll(selector));
    }
    return nodes;
  }

  function applyDocument(rootNode) {
    const documentRoot = rootNode || root.document;
    if (!documentRoot) return 0;
    let applied = 0;

    for (const element of translatableNodes(documentRoot, "[data-i18n]")) {
      const key = element.getAttribute("data-i18n");
      if (!key) continue;
      const original = element.getAttribute("data-i18n-original") || element.textContent || "";
      element.textContent = t(key, elementParams(element), original);
      applied += 1;
    }

    for (const element of translatableNodes(documentRoot, "[data-i18n-attr]")) {
      const specification = element.getAttribute("data-i18n-attr") || "";
      const params = elementParams(element);
      for (const entry of specification.split(";")) {
        const separator = entry.indexOf(":");
        if (separator < 1) continue;
        const attribute = entry.slice(0, separator).trim();
        const key = entry.slice(separator + 1).trim();
        if (!attribute || !key || unsafeAttributePattern.test(attribute)) continue;
        const original = element.getAttribute(attribute) || "";
        element.setAttribute(attribute, t(key, params, original));
        applied += 1;
      }
    }
    return applied;
  }

  function setDocumentLanguage() {
    if (root.document && root.document.documentElement) {
      root.document.documentElement.setAttribute("lang", currentLocale);
    }
  }

  function setLocale(locale) {
    const normalized = normalizeLocale(locale);
    if (!normalized || !supportedLocales.includes(normalized)) return currentLocale;
    currentLocale = normalized;
    setDocumentLanguage();
    if (root.document) applyDocument(root.document);
    return currentLocale;
  }

  function getLocale() {
    return currentLocale;
  }

  const allowedEnglish = Object.freeze({
    brand: Object.freeze([
      "YunOS",
      "Ayun English Core",
      "Codex",
      "DeepSeek",
      "Kimi",
      "ChatGPT",
      "Claude",
      "Gemini",
      "Ollama",
    ]),
    technical: Object.freeze([
      "AI",
      "Agent",
      "D1",
      "Git",
      "SQLite",
      "Tailscale",
      "PWA",
      "API",
      "Runtime",
      "Provider",
      "Token",
      "ID",
      "URL",
      "JSON",
      "Markdown",
      "TXT",
      "HTTP",
      "POST",
      "PUT",
      "PATCH",
      "DELETE",
      "UTF-8",
      "Omnibox",
      "Enter",
      "Cache-Control",
      "no-store",
      "real-readonly",
      "real",
      "web-real-readonly",
      "Runtime-admin",
      "profile-admin",
      "Gate B1",
      "Gate B2",
      "SHA-256",
      "commit",
      "tag",
      "branch",
      "mark/tag",
      "memory_summaries",
      "ISO 8601",
      "version identifiers",
      "snake_case API fields",
    ]),
    user: Object.freeze([
      "project names",
      "user messages",
      "AI responses",
      "imported content",
      "code",
      "commands",
      "logs",
      "file paths",
      "hashes",
      "event names",
      "API fields",
      "database enums",
    ]),
  });

  const api = Object.freeze({
    DEFAULT_LOCALE,
    FALLBACK_LOCALE,
    supportedLocales,
    catalogs,
    getLocale,
    setLocale,
    t,
    applyDocument,
    formatFullDate,
    formatShortDate,
    formatDateTime,
    formatRelativeTime,
    formatNumber,
    formatCount,
    formatFileSize,
    formatDuration,
    enumLabel,
    allowedEnglish,
  });

  Object.defineProperty(root, "YunOSI18n", {
    value: api,
    enumerable: true,
    configurable: false,
    writable: false,
  });

  function autoApply() {
    setDocumentLanguage();
    applyDocument(root.document);
  }

  if (root.document) {
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", autoApply, { once: true });
    } else {
      autoApply();
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
