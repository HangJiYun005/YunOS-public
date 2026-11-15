const YUNOS_I18N = globalThis.YunOSI18n;

function t(key, params = {}, fallback = "") {
  if (YUNOS_I18N?.t) return YUNOS_I18N.t(key, params, fallback);
  return fallback || "—";
}

function formatUiNumber(value, options = {}) {
  return YUNOS_I18N?.formatNumber
    ? YUNOS_I18N.formatNumber(value, options)
    : new Intl.NumberFormat("zh-CN", options).format(Number(value) || 0);
}

function formatUiDateTime(value, options = {}) {
  return YUNOS_I18N?.formatDateTime
    ? YUNOS_I18N.formatDateTime(value, options)
    : new Intl.DateTimeFormat("zh-CN", options).format(new Date(value));
}

function formatUiShortDate(value) {
  return YUNOS_I18N?.formatShortDate
    ? YUNOS_I18N.formatShortDate(value)
    : formatUiDateTime(value, { year: "numeric", month: "2-digit", day: "2-digit" });
}

function formatUiRelativeTime(value, base = Date.now()) {
  return YUNOS_I18N?.formatRelativeTime
    ? YUNOS_I18N.formatRelativeTime(value, base)
    : formatUiDateTime(value);
}

function formatUiFileSize(value) {
  return YUNOS_I18N?.formatFileSize
    ? YUNOS_I18N.formatFileSize(value)
    : `${formatUiNumber(value)} B`;
}

function formatUiDuration(value) {
  return YUNOS_I18N?.formatDuration
    ? YUNOS_I18N.formatDuration(value)
    : `${formatUiNumber(value)} ms`;
}

function enumLabel(group, value, context = "") {
  return YUNOS_I18N?.enumLabel
    ? YUNOS_I18N.enumLabel(group, value, context)
    : String(value ?? "");
}

const content = document.querySelector("#content");
const title = document.querySelector("#view-title");
const dbMode = document.querySelector("#db-mode");
const dbWarning = document.querySelector("#db-warning");
const desktopShell = document.querySelector("#yunos-desktop-shell");
const shellSidebar = document.querySelector("#shell-sidebar");
const shellSidebarToggle = document.querySelector("#shell-sidebar-toggle");
const shellSidebarEdge = document.querySelector(".yunos-shell-edge-slot");
const shellSidebarNav = document.querySelector("#shell-sidebar-nav");
const sidebarGlobalItems = document.querySelector("#sidebar-global-items");
const sidebarOperationalItems = document.querySelector("#sidebar-operational-items");
const sidebarContextGroup = document.querySelector("#sidebar-context-group");
const sidebarContextItems = document.querySelector("#sidebar-context-items");
const sidebarSystemItems = document.querySelector("#sidebar-system-items");
const sidebarSettingsItems = document.querySelector("#sidebar-settings-items");
const sidebarToolItems = document.querySelector("#sidebar-tool-items");
const shellRegions = document.querySelectorAll("[data-shell-region]");
const homeOmniboxForm = document.querySelector("#home-omnibox-form");
const homeOmniboxInput = document.querySelector("#home-omnibox-input");
const homeOmniboxModeButtons = document.querySelectorAll("[data-omnibox-mode]");
const homeMemorySearchOverlay = document.querySelector("#home-memory-search-overlay");
const homeMemorySearchResults = document.querySelector("#home-memory-search-results");
const homeAiDocument = document.querySelector("#home-ai-document");
const homeAiDocumentStatus = document.querySelector("#home-ai-document-status");
const homeAiDocumentContent = document.querySelector("#home-ai-document-content");
const mobileShell = document.querySelector("#yunos-mobile-shell");
const mobileSearchForm = document.querySelector("#mobile-search-form");
const mobileSearchInput = document.querySelector("#mobile-search-input");
const mobileSearchClear = document.querySelector("#mobile-search-clear");
const mobileSearchHint = document.querySelector("#mobile-search-hint");
const mobileOfflineHome = document.querySelector("#mobile-offline-home");
const mobileOfflineStatus = document.querySelector("#mobile-offline-status");
const mobileOfflineConversations = document.querySelector("#mobile-offline-conversations");
const mobileResultsPage = document.querySelector("#mobile-results-page");
const mobileResultsSummary = document.querySelector("#mobile-results-summary");
const mobileSortToggle = document.querySelector("#mobile-sort-toggle");
const mobileOfflineSearchBanner = document.querySelector("#mobile-offline-search-banner");
const mobileOfflineSearchCoverage = document.querySelector("#mobile-offline-search-coverage");
const mobileOfflineSearchUpdated = document.querySelector("#mobile-offline-search-updated");
const mobileSearchAllHost = document.querySelector("#mobile-search-all-host");
const mobileSearchStatus = document.querySelector("#mobile-search-status");
const mobileSearchResults = document.querySelector("#mobile-search-results");
const mobileSearchSentinel = document.querySelector("#mobile-search-sentinel");
const mobileLoadMore = document.querySelector("#mobile-load-more");
const mobileRecentButton = document.querySelector("#mobile-recent-button");
const mobileOperationalButton = document.querySelector("#mobile-operational-button");
const mobileOperationalPanel = document.querySelector("#mobile-operational-panel");
const mobileOperationalClose = document.querySelector("#mobile-operational-close");
const mobileOperationalTitle = document.querySelector("#mobile-operational-title");
const mobileOperationalNav = document.querySelector("#mobile-operational-nav");
const mobileOperationalContent = document.querySelector("#mobile-operational-content");
const mobileCognitiveButton = document.querySelector("#mobile-cognitive-button");
const mobileCognitivePanel = document.querySelector("#mobile-cognitive-panel");
const mobileCognitiveClose = document.querySelector("#mobile-cognitive-close");
const mobileCognitiveTitle = document.querySelector("#mobile-cognitive-title");
const mobileCognitiveContent = document.querySelector("#mobile-cognitive-content");
const mobileRecentBackdrop = document.querySelector("#mobile-recent-backdrop");
const mobileRecentDrawer = document.querySelector("#mobile-recent-drawer");
const mobileRecentDrawerHandle = mobileRecentDrawer?.querySelector(".yunos-mobile-drawer-handle");
const mobileRecentStatus = document.querySelector("#mobile-recent-status");
const mobileRecentClear = document.querySelector("#mobile-recent-clear");
const mobileRecentSearches = document.querySelector("#mobile-recent-searches");
const mobileRecentConversations = document.querySelector("#mobile-recent-conversations");
const mobileConversationReader = document.querySelector("#mobile-conversation-reader");
const mobileReaderTopbar = document.querySelector("#mobile-reader-topbar");
const mobileReaderBack = document.querySelector("#mobile-reader-back");
const mobileReaderTitle = document.querySelector("#mobile-reader-title");
const mobileReaderHideTopbar = document.querySelector("#mobile-reader-hide-topbar");
const mobileReaderRestoreTopbar = document.querySelector("#mobile-reader-restore-topbar");
const mobileReaderScroll = document.querySelector("#mobile-reader-scroll");
const mobileReaderOfflineBanner = document.querySelector("#mobile-reader-offline-banner");
const mobileReaderCacheTime = document.querySelector("#mobile-reader-cache-time");
const mobileReaderRevalidate = document.querySelector("#mobile-reader-revalidate");
const mobileReaderTopStatus = document.querySelector("#mobile-reader-top-status");
const mobileReaderMessages = document.querySelector("#mobile-reader-messages");
const mobileReaderBottomStatus = document.querySelector("#mobile-reader-bottom-status");
const mobileCacheToast = document.querySelector("#mobile-cache-toast");

const HOME_OMNIBOX_STORAGE_KEY = "yunos.homeOmnibox.mode";
const HOME_OMNIBOX_DEFAULT_MODE = "search";
const HOME_OMNIBOX_MODES = new Set(["search", "ai"]);
const HOME_MEMORY_SEARCH_LIMIT = 8;
const MOBILE_SEARCH_DEBOUNCE_MS = 1000;
const MOBILE_SEARCH_PAGE_SIZE = 20;
const MOBILE_SEARCH_AUTO_LIMIT = 60;
const MOBILE_SEARCH_HISTORY_KEY = "yunos.mobileSearch.history.v1";
const MOBILE_SEARCH_HISTORY_LIMIT = 12;
const MOBILE_READER_PAGE_SIZE = 20;
const MOBILE_MESSAGE_REQUEST_TIMEOUT_MS = 12_000;
const MOBILE_ACTIVE_CACHE_REFRESH_MS = 10_000;
const MOBILE_STATE_SAVE_DEBOUNCE_MS = 320;
const MOBILE_SCROLL_SAVE_DEBOUNCE_MS = 180;
const HOME_AI_SESSION_KEY = "yunos.homeAiDocument.session.v1";
const HOME_AI_BUDGET_PROFILE = "Small";
const HOME_AI_RECEIVING_DELAY_MS = 420;
const COGNITIVE_STALL_THRESHOLD_MS = 20 * 60 * 1000;
const COGNITIVE_RUNS_ENDPOINT = "/api/cognitive/runs?limit=50";
const COGNITIVE_WORKSPACE_BUNDLE_ENDPOINT = "/api/cognitive/workspace";
const SIDEBAR_HOVER_OPEN_DELAY_MS = 180;
const SIDEBAR_HOVER_CLOSE_DELAY_MS = 260;
const HOME_AI_PHASES = Object.freeze({
  IDLE: "idle",
  PREPARING_PREVIEW: "preparing-preview",
  PREVIEW_READY: "preview-ready",
  EXECUTING: "executing",
  RECEIVING_RESULT: "receiving-result",
  SUCCESS: "success",
  TRUNCATED: "truncated",
  INSUFFICIENT_EVIDENCE: "insufficient-evidence",
  PROVIDER_ERROR: "provider-error",
  CANCELLED_OR_UNAVAILABLE: "cancelled-or-unavailable",
});
const HOME_OMNIBOX_PLACEHOLDERS = {
  search: () => t("search.memory", {}, "搜索记忆"),
  ai: () => t("ai.askYunOS", {}, "向 YunOS 提问"),
};
const homeMemorySearchState = {
  query: "",
  items: [],
  activeIndex: -1,
  requestSerial: 0,
  open: false,
};
const mobileSearchState = {
  query: "",
  submittedQuery: "",
  sort: "relevance",
  groups: [],
  totalConversations: 0,
  hasMore: false,
  nextOffset: 0,
  loading: false,
  requestSerial: 0,
  abortController: null,
  debounceTimer: 0,
  completedFirstPageKey: "",
  activeRequestKey: "",
  paginationError: false,
  autoLoadPaused: false,
  expandedConversationId: "",
  expandedPreviewKey: "",
  previewCache: new Map(),
  previewAbortController: null,
  previewRequestSerial: 0,
  resultsScrollTop: 0,
  viewportBaseline: 0,
  keyboardWasOpen: false,
  keyboardDismissedWhileFocused: false,
  lastFocusedElement: null,
  drawerCloseTimer: 0,
  sourceMode: "online",
  cacheCoverage: {
    conversationCount: 0,
    messageCount: 0,
    lastUpdatedAt: 0,
  },
  resultRefs: [],
  searchSnapshotKey: "",
  restoring: false,
  restoreTarget: null,
};
const mobileConversationState = {
  conversation: null,
  conversationId: "",
  focusMessageId: "",
  query: "",
  messages: [],
  messageIds: new Set(),
  beforeCursor: "",
  afterCursor: "",
  hasBefore: false,
  hasAfter: false,
  loadingInitial: false,
  loadingBefore: false,
  loadingAfter: false,
  initialError: "",
  beforeError: "",
  afterError: "",
  initialTechnicalDetail: "",
  beforeTechnicalDetail: "",
  afterTechnicalDetail: "",
  requestSerial: { initial: 0, before: 0, after: 0 },
  abortControllers: { initial: null, before: null, after: null },
  positioned: false,
  suppressEdgeLoading: false,
  scrollTop: 0,
  activeKey: "",
  snapshots: new Map(),
  topbarVisibility: new Map(),
  expandedTraceIds: new Set(),
  offline: false,
  cachedAt: 0,
  reachedStart: "unknown",
  reachedEnd: "unknown",
  gapsBeforeMessageIds: new Set(),
  cachedSegmentKeys: [],
  activeProtectionTimer: 0,
  revalidating: false,
  preserveScrollTopOnReload: null,
};
const mobileStatePersistence = {
  saveTimer: 0,
  scrollTimer: 0,
  restoredSavedAt: 0,
  lastInteractionAt: 0,
  hasUserInteraction: false,
  restoreInProgress: false,
  pendingRestore: null,
  lastFailureReason: "",
  suppressInteractionUntil: 0,
};
const homeAiState = {
  phase: HOME_AI_PHASES.IDLE,
  question: "",
  budget: HOME_AI_BUDGET_PROFILE,
  requestId: "",
  mutationId: "",
  modelRunId: "",
  preview: null,
  envelope: null,
  executionAttempted: false,
  inFlight: false,
  requestSerial: 0,
  receivingTimer: 0,
  cognitiveBundle: null,
  cognitiveBundleStatus: "idle",
  cognitiveBundleDetail: "",
  runtimeFailure: null,
};
let legacyViewRequestSerial = 0;
let mobileOperationalPreviousView = "home";
let mobileCognitivePreviousView = "home";
let mobileCognitiveRequestSerial = 0;

const SIDEBAR_ICON_PATHS = Object.freeze({
  memory: ["M6 4.5h10.5A2.5 2.5 0 0 1 19 7v12H8.5A2.5 2.5 0 0 0 6 21.5z", "M6 4.5H5A2 2 0 0 0 3 6.5v12A2.5 2.5 0 0 1 5.5 21H8"],
  timeline: ["M5 4v3", "M19 4v3", "M4 8h16", "M5 6h14a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z", "M8 12h3", "M8 16h6"],
  profile: ["M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M4.5 21a7.5 7.5 0 0 1 15 0"],
  projects: ["M3 7.5h7l2 2h9v9.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z", "M3 7.5V6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v1.5"],
  now: ["M4 5h16v14H4z", "M8 9h8", "M8 13h5"],
  waiting: ["M12 7v5l3 2", "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z"],
  decisions: ["M5 4h14v16H5z", "M8 8h8", "M8 12h8", "M8 16h5"],
  delegations: ["M7 7h10v10H7z", "M12 2v5", "M12 17v5", "M2 12h5", "M17 12h5"],
  runtime: ["M3 12h4l2.2-5 4.1 10 2.3-5H21"],
  settings: ["M4 7h10", "M18 7h2", "M14 7a2 2 0 1 0 4 0 2 2 0 0 0-4 0z", "M4 17h2", "M10 17h10", "M6 17a2 2 0 1 0 4 0 2 2 0 0 0-4 0z"],
  tools: ["M14.5 6.5a4 4 0 0 0-5.2 5.2L4 17l3 3 5.3-5.3a4 4 0 0 0 5.2-5.2l-2.4-2.4z"],
  query: ["M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z", "m16 16 5 5"],
  source: ["M5 5h14v14H5z", "M8 9h8", "M8 13h8", "M8 17h5"],
  uncertainty: ["M12 17h.01", "M9.6 9a2.5 2.5 0 1 1 3.4 2.3c-.7.3-1 .8-1 1.7v.5", "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z"],
  execution: ["M4 5h16v14H4z", "M8 9h8", "M8 13h5"],
});

const SIDEBAR_NAVIGATION = Object.freeze({
  global: [
    { key: "memory", labelKey: "nav.memory", fallback: "记忆", view: "search", icon: "memory" },
    { key: "timeline", labelKey: "nav.timeline", fallback: "时间轴", view: "timeline", icon: "timeline" },
    { key: "profile", labelKey: "nav.profile", fallback: "画像", view: "approvedProfiles", icon: "profile" },
    { key: "projects", labelKey: "nav.projects", fallback: "项目", view: "projects", icon: "projects" },
  ],
  operational: [
    { key: "operational-now", labelKey: "nav.now", fallback: "当前", view: "operationalNow", icon: "now" },
    { key: "operational-projects", labelKey: "nav.projects", fallback: "项目", view: "operationalProjects", icon: "projects" },
    { key: "operational-waiting", labelKey: "nav.waiting", fallback: "等待", view: "operationalWaiting", icon: "waiting" },
    { key: "operational-decisions", labelKey: "nav.decisions", fallback: "决策", view: "operationalDecisions", icon: "decisions" },
    { key: "operational-delegations", labelKey: "nav.delegations", fallback: "委托", view: "operationalDelegations", icon: "delegations" },
  ],
  system: [
    { key: "runtime", labelKey: "nav.runtime", fallback: "运行", view: "runtimeStatus", icon: "runtime" },
  ],
  settings: [
    { key: "marks", labelKey: "nav.statusManagement", fallback: "状态管理", view: "marks" },
    { key: "tags", labelKey: "nav.tagManagement", fallback: "标签管理", view: "tags" },
  ],
  tools: [
    { key: "runtimeExecution", labelKey: "runtime.execution", fallback: "Runtime 执行", view: "runtimeExecution" },
    { key: "recent", labelKey: "mobile.recentRecords", fallback: "最近记录", view: "recent" },
    { key: "summaries", labelKey: "nav.memorySummaries", fallback: "记忆摘要", view: "summaries" },
    { key: "contextReview", labelKey: "context.preview", fallback: "上下文预览", view: "contextReview" },
    { key: "promptInspector", labelKey: "context.inspector", fallback: "上下文检查器", view: "promptInspector" },
    { key: "contextOptimizer", labelKey: "context.optimizer", fallback: "上下文优化器", view: "contextOptimizer" },
    { key: "about", labelKey: "nav.about", fallback: "关于系统", view: "about" },
  ],
});

const shellSidebarState = {
  pinned: desktopShell?.dataset.sidebarPinned === "true",
  hover: false,
  pointerInside: false,
  openTimer: 0,
  closeTimer: 0,
};

function createSidebarIcon(iconName) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("yunos-sidebar-icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  (SIDEBAR_ICON_PATHS[iconName] || []).forEach((pathData) => {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", pathData);
    svg.appendChild(path);
  });
  return svg;
}

function createSidebarViewItem(item, { nested = false } = {}) {
  const buttonElement = document.createElement("button");
  buttonElement.className = `nav-item yunos-sidebar-item${nested ? " is-nested" : ""}`;
  buttonElement.type = "button";
  buttonElement.dataset.view = item.view;
  buttonElement.dataset.navKey = item.key;
  if (item.icon) buttonElement.appendChild(createSidebarIcon(item.icon));
  const label = document.createElement("span");
  label.className = "yunos-sidebar-item-label";
  label.textContent = t(item.labelKey, {}, item.fallback);
  buttonElement.appendChild(label);
  return buttonElement;
}

function renderSidebarNavigation() {
  const groups = [
    [sidebarGlobalItems, SIDEBAR_NAVIGATION.global, false],
    [sidebarOperationalItems, SIDEBAR_NAVIGATION.operational, false],
    [sidebarSystemItems, SIDEBAR_NAVIGATION.system, false],
    [sidebarSettingsItems, SIDEBAR_NAVIGATION.settings, true],
    [sidebarToolItems, SIDEBAR_NAVIGATION.tools, true],
  ];
  groups.forEach(([target, items, nested]) => {
    if (!target) return;
    target.replaceChildren(...items.map((item) => createSidebarViewItem(item, { nested })));
  });
  shellSidebarNav?.querySelectorAll("[data-sidebar-summary-icon]").forEach((summary) => {
    summary.prepend(createSidebarIcon(summary.dataset.sidebarSummaryIcon));
  });
}

renderSidebarNavigation();

function createSidebarContextItem({ key, label, meta = "", icon = "query", activate }) {
  const buttonElement = document.createElement("button");
  buttonElement.className = "yunos-sidebar-context-item";
  buttonElement.type = "button";
  buttonElement.dataset.contextKey = key;
  buttonElement.appendChild(createSidebarIcon(icon));
  const copy = document.createElement("span");
  copy.className = "yunos-sidebar-context-copy";
  const labelElement = document.createElement("span");
  labelElement.className = "yunos-sidebar-context-label";
  labelElement.textContent = label;
  copy.appendChild(labelElement);
  if (meta !== "") {
    const metaElement = document.createElement("span");
    metaElement.className = "yunos-sidebar-context-meta";
    metaElement.textContent = String(meta);
    metaElement.title = String(meta);
    copy.appendChild(metaElement);
  }
  buttonElement.appendChild(copy);
  buttonElement.addEventListener("click", () => {
    sidebarContextItems?.querySelectorAll(".is-active").forEach((item) => item.classList.remove("is-active"));
    buttonElement.classList.add("is-active");
    activate();
  });
  return buttonElement;
}

function focusSidebarTarget(target, { openDetails = false } = {}) {
  if (!(target instanceof HTMLElement)) return;
  if (openDetails && target instanceof HTMLDetailsElement) target.open = true;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  const focusTarget = target instanceof HTMLDetailsElement ? target.querySelector("summary") : target;
  if (focusTarget instanceof HTMLElement) {
    if (!focusTarget.matches("button, input, select, textarea, summary, [tabindex]")) {
      focusTarget.tabIndex = -1;
    }
    focusTarget.focus({ preventScroll: true });
  }
}

function sidebarAiDisclosureItem(key, label, count, icon) {
  return {
    key,
    label,
    meta: count,
    icon,
    activate: () => focusSidebarTarget(document.querySelector(`#home-ai-section-${key}`), { openDetails: true }),
  };
}

function currentSidebarContextItems() {
  const pageState = desktopShell?.dataset.pageState || "home";
  if (pageState === "home") {
    const query = String(homeMemorySearchState.query || "").trim();
    if (!homeMemorySearchState.open || !query) return [];
    const items = [{
      key: "current-query",
      label: t("search.currentQuery", {}, "当前查询"),
      meta: query,
      icon: "query",
      activate: () => homeOmniboxInput?.focus(),
    }];
    if (homeMemorySearchState.items.length) {
      items.push({
        key: "related-memories",
        label: t("ai.relatedMemories", {}, "相关记忆"),
        meta: homeMemorySearchState.items.length,
        icon: "memory",
        activate: () => focusSidebarTarget(homeMemorySearchResults?.querySelector("[role='option']") || homeMemorySearchResults),
      });
    }
    return items;
  }
  if (pageState !== "ai") return [];

  const items = [];
  const question = String(homeAiState.question || "").trim();
  if (question) {
    items.push({
      key: "current-task",
      label: t("ai.currentTask", {}, "当前任务"),
      meta: question,
      icon: "query",
      activate: () => homeOmniboxInput?.focus(),
    });
  }
  if (homeAiState.preview) {
    items.push(
      sidebarAiDisclosureItem("sources", t("ai.sources", {}, "来源"), homeAiEvidenceItems().length, "source"),
      sidebarAiDisclosureItem("memories", t("ai.relatedMemories", {}, "相关记忆"), homeAiProfileItems().length, "memory"),
      sidebarAiDisclosureItem("uncertainties", t("ai.uncertainties", {}, "不确定信息"), homeAiUncertaintyItems().length, "uncertainty"),
      sidebarAiDisclosureItem(
        "execution",
        t("ai.executionDetails", {}, "执行详情"),
        homeAiState.envelope
          ? t("status.reported", {}, "已记录")
          : t("common.preview", {}, "预览"),
        "execution",
      ),
    );
  }
  return items;
}

function renderSidebarContext() {
  if (!sidebarContextGroup || !sidebarContextItems) return;
  const items = currentSidebarContextItems();
  sidebarContextItems.replaceChildren(...items.map((item) => createSidebarContextItem(item)));
  sidebarContextGroup.hidden = items.length === 0;
}

function readHomeOmniboxMode() {
  try {
    const storedMode = window.localStorage.getItem(HOME_OMNIBOX_STORAGE_KEY);
    return HOME_OMNIBOX_MODES.has(storedMode) ? storedMode : HOME_OMNIBOX_DEFAULT_MODE;
  } catch {
    return HOME_OMNIBOX_DEFAULT_MODE;
  }
}

function persistHomeOmniboxMode(mode) {
  try {
    window.localStorage.setItem(HOME_OMNIBOX_STORAGE_KEY, mode);
  } catch {
    // Storage may be disabled or unavailable; the in-page mode remains usable.
  }
}

function setHomeOmniboxMode(mode, { persist = false } = {}) {
  const nextMode = HOME_OMNIBOX_MODES.has(mode) ? mode : HOME_OMNIBOX_DEFAULT_MODE;
  const placeholder = HOME_OMNIBOX_PLACEHOLDERS[nextMode]();

  if (homeOmniboxForm) homeOmniboxForm.dataset.mode = nextMode;
  if (homeOmniboxInput) {
    homeOmniboxInput.placeholder = placeholder;
    homeOmniboxInput.setAttribute("aria-label", placeholder);
  }
  homeOmniboxModeButtons.forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.omniboxMode === nextMode));
  });
  if (nextMode !== "search") closeHomeMemorySearchOverlay({ invalidateRequest: true });
  if (persist) persistHomeOmniboxMode(nextMode);
  return nextMode;
}

function syncHomeOmniboxValueState() {
  if (!homeOmniboxForm || !homeOmniboxInput) return;
  homeOmniboxForm.dataset.hasValue = String(homeOmniboxInput.value.length > 0);
}

function setHomeMemorySearchExpanded(expanded) {
  if (!homeMemorySearchOverlay || !homeOmniboxInput) return;
  const isExpanded = Boolean(expanded);
  homeMemorySearchState.open = isExpanded;
  homeMemorySearchOverlay.hidden = !isExpanded;
  homeOmniboxInput.setAttribute("aria-expanded", String(isExpanded));
  if (!isExpanded) homeOmniboxInput.removeAttribute("aria-activedescendant");
  renderSidebarContext();
}

function closeHomeMemorySearchOverlay({ invalidateRequest = false } = {}) {
  if (invalidateRequest) homeMemorySearchState.requestSerial += 1;
  homeMemorySearchState.activeIndex = -1;
  setHomeMemorySearchExpanded(false);
}

function technicalDetailsElement(detail, className = "") {
  const technicalDetail = String(detail || "").trim();
  if (!technicalDetail) return null;
  const disclosure = document.createElement("details");
  if (className) disclosure.className = className;
  const summary = document.createElement("summary");
  summary.textContent = t("common.technicalDetails", {}, "技术详情");
  const code = document.createElement("code");
  code.textContent = technicalDetail;
  disclosure.append(summary, code);
  return disclosure;
}

function renderHomeMemorySearchStatus(message, kind = "status", technicalDetail = "") {
  if (!homeMemorySearchResults || !homeMemorySearchOverlay) return;
  homeMemorySearchState.items = [];
  homeMemorySearchState.activeIndex = -1;
  homeMemorySearchResults.replaceChildren();
  const status = document.createElement("div");
  status.className = `yunos-home-memory-search-status${kind === "error" ? " is-error" : ""}`;
  status.setAttribute("role", kind === "error" ? "alert" : "status");
  const copy = document.createElement("span");
  copy.textContent = message;
  status.appendChild(copy);
  if (kind === "error") {
    const disclosure = technicalDetailsElement(
      technicalDetail,
      "yunos-mobile-reader-error-details",
    );
    if (disclosure) status.appendChild(disclosure);
  }
  homeMemorySearchResults.appendChild(status);
  homeMemorySearchOverlay.setAttribute("aria-busy", String(kind === "loading"));
  setHomeMemorySearchExpanded(true);
}

function homeMemorySearchAnchorLabel(item) {
  if (item.message_id) {
    return t("search.messageAnchor", { id: item.message_id }, `消息 ${item.message_id}`);
  }
  if (item.conversation_id) {
    return t("search.conversationAnchor", { id: item.conversation_id }, `会话 ${item.conversation_id}`);
  }
  return t("search.sourceAnchorUnavailable", {}, "来源锚点不可用");
}

function formatHomeMemorySearchTime(value) {
  const raw = String(value || "").trim();
  if (!raw) return t("date.noTime", {}, "无时间");
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return raw;
  return formatUiDateTime(parsed, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function setHomeMemorySearchActive(index, { scroll = false } = {}) {
  const options = [...(homeMemorySearchResults?.querySelectorAll("[role='option']") || [])];
  if (!options.length) {
    homeMemorySearchState.activeIndex = -1;
    homeOmniboxInput?.removeAttribute("aria-activedescendant");
    return;
  }
  const nextIndex = ((index % options.length) + options.length) % options.length;
  homeMemorySearchState.activeIndex = nextIndex;
  options.forEach((option, optionIndex) => {
    const isActive = optionIndex === nextIndex;
    option.setAttribute("aria-selected", String(isActive));
    option.classList.toggle("is-active", isActive);
  });
  homeOmniboxInput?.setAttribute("aria-activedescendant", options[nextIndex].id);
  if (scroll) options[nextIndex].scrollIntoView({ block: "nearest" });
}

function openHomeMemorySearchResult(index = homeMemorySearchState.activeIndex) {
  const item = homeMemorySearchState.items[index];
  if (!item?.conversation_id) {
    renderHomeMemorySearchStatus(t(
      "search.resultMissingAnchor",
      {},
      "该结果缺少可打开的来源锚点。",
    ), "error");
    return;
  }
  closeHomeMemorySearchOverlay();
  setShellPageState("workspace");
  loadConversation(item.conversation_id, {
    focusMessageId: item.message_id || "",
    fromHomeSearch: true,
  });
}

function renderHomeMemorySearchResults(items, query) {
  if (!homeMemorySearchResults || !homeMemorySearchOverlay) return;
  homeMemorySearchState.query = query;
  homeMemorySearchState.items = items;
  homeMemorySearchResults.replaceChildren();
  homeMemorySearchOverlay.setAttribute("aria-busy", "false");
  if (!items.length) {
    renderHomeMemorySearchStatus(t(
      "search.noLocalMatches",
      {},
      "没有找到匹配的本地记忆。",
    ));
    return;
  }

  const seenConversationIds = new Set();
  items.forEach((item, index) => {
    const conversationId = item.conversation_id || "";
    const repeatedConversation = Boolean(conversationId && seenConversationIds.has(conversationId));
    if (conversationId) seenConversationIds.add(conversationId);
    const option = document.createElement("button");
    option.id = `home-memory-search-result-${index}`;
    option.className = "yunos-home-memory-search-result";
    option.classList.toggle("is-repeated-conversation", repeatedConversation);
    option.type = "button";
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    option.title = buildReference(item) || homeMemorySearchAnchorLabel(item);

    const heading = document.createElement("span");
    heading.className = "yunos-home-memory-search-result-title";
    appendHighlighted(
      heading,
      item.title || t("search.untitledConversation", {}, "未命名对话"),
      query,
    );

    const snippet = document.createElement("span");
    snippet.className = "yunos-home-memory-search-result-snippet";
    appendHighlighted(
      snippet,
      item.snippet || t("search.noMatchingSummary", {}, "无匹配摘要"),
      query,
    );

    const meta = document.createElement("span");
    meta.className = "yunos-home-memory-search-result-meta";
    const source = labelSourcePlatform(item.source_platform, item.source_platform_label);
    const timestamp = formatHomeMemorySearchTime(item.create_time_iso || item.update_time_iso);
    const sourceMeta = document.createElement("span");
    sourceMeta.textContent = t(
      "shell.extra.sourceAndTime",
      { source, time: timestamp },
      `${source} · ${timestamp}`,
    );
    const anchor = document.createElement("span");
    anchor.className = "yunos-home-memory-search-result-anchor";
    anchor.textContent = homeMemorySearchAnchorLabel(item);
    anchor.title = buildReference(item) || anchor.textContent;
    meta.append(sourceMeta, anchor);

    option.append(heading, snippet, meta);
    option.addEventListener("pointermove", () => setHomeMemorySearchActive(index));
    option.addEventListener("click", () => openHomeMemorySearchResult(index));
    homeMemorySearchResults.appendChild(option);
  });
  setHomeMemorySearchExpanded(true);
  setHomeMemorySearchActive(0);
}

async function handleHomeMemorySearchSubmit(event) {
  if (event.detail?.mode !== "search") return;
  const query = String(event.detail?.query || "").trim();
  if (!query) {
    homeMemorySearchState.requestSerial += 1;
    homeMemorySearchState.query = "";
    renderHomeMemorySearchStatus(t("search.enterKeywords", {}, "请输入搜索关键词。"), "error");
    homeOmniboxInput?.focus();
    return;
  }

  const requestSerial = ++homeMemorySearchState.requestSerial;
  homeMemorySearchState.query = query;
  renderHomeMemorySearchStatus(t("search.searchingLocal", {}, "正在搜索本地记忆…"), "loading");
  try {
    const data = await api(
      `/api/search?q=${encodeURIComponent(query)}&limit=${HOME_MEMORY_SEARCH_LIMIT}&memory_space=whale`,
    );
    if (
      requestSerial !== homeMemorySearchState.requestSerial
      || homeOmniboxForm?.dataset.mode !== "search"
    ) return;
    renderHomeMemorySearchResults(Array.isArray(data.items) ? data.items : [], query);
  } catch (error) {
    if (requestSerial !== homeMemorySearchState.requestSerial) return;
    renderHomeMemorySearchStatus(
      t("search.localSearchFailed", {}, "本地记忆搜索失败，请检查本地服务后重试。"),
      "error",
      error?.technicalDetail || error?.message || "MEMORY_SEARCH_FAILED",
    );
  }
}

function homeAiIsWorking(phase = homeAiState.phase) {
  return [
    HOME_AI_PHASES.PREPARING_PREVIEW,
    HOME_AI_PHASES.EXECUTING,
    HOME_AI_PHASES.RECEIVING_RESULT,
  ].includes(phase);
}

function clearHomeAiReceivingTimer() {
  if (!homeAiState.receivingTimer) return;
  window.clearTimeout(homeAiState.receivingTimer);
  homeAiState.receivingTimer = 0;
}

function setHomeAiInputLocked(locked) {
  if (homeOmniboxInput) homeOmniboxInput.readOnly = Boolean(locked);
  homeOmniboxModeButtons.forEach((buttonElement) => {
    buttonElement.disabled = Boolean(locked);
  });
}

function setHomeAiPhase(phase, statusMessage = "") {
  const nextPhase = Object.values(HOME_AI_PHASES).includes(phase)
    ? phase
    : HOME_AI_PHASES.CANCELLED_OR_UNAVAILABLE;
  homeAiState.phase = nextPhase;
  const working = homeAiIsWorking(nextPhase);
  if (desktopShell) desktopShell.dataset.aiState = nextPhase;
  if (homeOmniboxForm) homeOmniboxForm.dataset.aiWorking = String(working);
  if (homeAiDocument) {
    homeAiDocument.hidden = nextPhase === HOME_AI_PHASES.IDLE;
    homeAiDocument.dataset.aiState = nextPhase;
    homeAiDocument.setAttribute("aria-busy", String(working));
  }
  if (homeAiDocumentStatus) homeAiDocumentStatus.textContent = statusMessage;
  renderSidebarContext();
}

function clearHomeAiSession() {
  try {
    window.sessionStorage.removeItem(HOME_AI_SESSION_KEY);
  } catch {
    // Session storage is optional; Runtime remains the source of truth.
  }
}

function persistHomeAiSession() {
  if (!homeAiState.preview || !homeAiState.modelRunId) return;
  const payload = {
    version: 1,
    question: homeAiState.question,
    budget: homeAiState.budget,
    requestId: homeAiState.requestId,
    mutationId: homeAiState.mutationId,
    modelRunId: homeAiState.modelRunId,
    executionAttempted: homeAiState.executionAttempted,
  };
  try {
    window.sessionStorage.setItem(HOME_AI_SESSION_KEY, JSON.stringify(payload));
  } catch {
    // Refresh restoration becomes unavailable without affecting the current run.
  }
}

function readHomeAiSession() {
  let raw = "";
  try {
    raw = window.sessionStorage.getItem(HOME_AI_SESSION_KEY) || "";
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const value = JSON.parse(raw);
    const idPattern = /^[A-Za-z0-9._:-]{8,128}$/;
    const runPattern = /^run_[0-9a-f]{64}$/;
    if (
      value?.version !== 1
      || typeof value.question !== "string"
      || normalizeRuntimeQuestion(value.question) !== value.question
      || !["Small", "Medium", "Large"].includes(value.budget)
      || !idPattern.test(value.requestId)
      || !idPattern.test(value.mutationId)
      || !runPattern.test(value.modelRunId)
      || typeof value.executionAttempted !== "boolean"
    ) {
      clearHomeAiSession();
      return null;
    }
    return value;
  } catch {
    clearHomeAiSession();
    return null;
  }
}

function resetHomeAiState() {
  clearHomeAiReceivingTimer();
  homeAiState.requestSerial += 1;
  Object.assign(homeAiState, {
    phase: HOME_AI_PHASES.IDLE,
    question: "",
    budget: HOME_AI_BUDGET_PROFILE,
    requestId: "",
    mutationId: "",
    modelRunId: "",
    preview: null,
    envelope: null,
    executionAttempted: false,
    inFlight: false,
    cognitiveBundle: null,
    cognitiveBundleStatus: "idle",
    cognitiveBundleDetail: "",
    runtimeFailure: null,
  });
  clearHomeAiSession();
  if (homeAiDocumentContent) homeAiDocumentContent.replaceChildren();
  setHomeAiInputLocked(false);
  setHomeAiPhase(HOME_AI_PHASES.IDLE);
}

function homeAiHeader(kicker, heading, lead) {
  const header = el("header", "yunos-ai-document-header");
  header.append(
    el("p", "yunos-ai-document-kicker", kicker),
    el("h1", "yunos-ai-document-title", heading),
  );
  if (lead) header.appendChild(el("p", "yunos-ai-document-lead", lead));
  return header;
}

function homeAiSummaryItem(label, value) {
  const item = el("div", "yunos-ai-summary-item");
  item.append(
    el("span", "yunos-ai-summary-label", label),
    el("strong", "yunos-ai-summary-value", String(
      value ?? t("status.unavailable", {}, "不可用"),
    )),
  );
  return item;
}

function homeAiDetailItem(label, value) {
  const item = el("div", "yunos-ai-detail-item");
  item.append(
    el("span", "yunos-ai-detail-label", label),
    el("span", "yunos-ai-detail-value", String(
      value ?? t("status.unavailable", {}, "不可用"),
    )),
  );
  return item;
}

function homeAiEvidenceItems(preview = homeAiState.preview) {
  const items = preview?.delivery?.layers?.historical_evidence?.items;
  return Array.isArray(items) ? items : [];
}

function homeAiProfileItems(preview = homeAiState.preview) {
  const items = preview?.delivery?.layers?.approved_profile?.items;
  return Array.isArray(items) ? items : [];
}

function homeAiUncertaintyItems(preview = homeAiState.preview) {
  const items = preview?.delivery?.uncertainties;
  return Array.isArray(items) ? items : [];
}

function homeAiExternalApiLabel(preview) {
  if (!homeAiEvidenceItems(preview).length) {
    return t("ai.externalApiNoEvidence", {}, "不会（证据不足）");
  }
  if (state.runtimeReview) return t("ai.externalApiReadonly", {}, "不会（只读复核）");
  if (!state.canExecute) return t("ai.externalApiNoAuthority", {}, "不会（无执行 authority）");
  if (preview?.runtime_sidecar?.ready !== true) {
    return t("ai.externalApiRuntimeNotReady", {}, "不会（Runtime sidecar 未就绪）");
  }
  if (state.runtimeMock) return t("ai.externalApiMock", {}, "不会（本地模拟传输）");
  if (state.canExecute) {
    return t("ai.externalApiOneDeepSeekCall", {}, "执行后会调用一次 DeepSeek");
  }
  return t("ai.externalApiUnavailable", {}, "当前服务不可调用");
}

function appendHomeAiExecutionSummary(target, preview) {
  const profile = preview?.runtime_profile || preview?.request_artifact?.runtime_profile || {};
  const evidence = homeAiEvidenceItems(preview);
  const summary = el("section", "yunos-ai-execution-summary");
  [
    [
      t("ai.memoryScope", {}, "记忆范围"),
      t("ai.whaleMemoryScope", {}, "鲸鱼记忆：已批准画像 + 历史证据"),
    ],
    [
      t("ai.evidence", {}, "证据"),
      t(
        "ai.sourceAnchorCount",
        { count: formatUiNumber(evidence.length) },
        `${formatUiNumber(evidence.length)} 条来源锚点`,
      ),
    ],
    [
      t("ai.approvedProfile", {}, "已批准画像"),
      `${preview?.review?.approved_profile_included ?? 0}/${preview?.review?.approved_profile_available ?? 0}`,
    ],
    [t("ai.model", {}, "模型"), profile.model || t("status.unavailable", {}, "不可用")],
    [
      t("ai.inputTokenEstimate", {}, "输入 token 估算"),
      preview?.review?.estimated_tokens ?? t("status.unavailable", {}, "不可用"),
    ],
    [t("ai.externalApi", {}, "外部 API"), homeAiExternalApiLabel(preview)],
  ].forEach(([label, value]) => summary.appendChild(homeAiSummaryItem(label, value)));
  target.appendChild(summary);
}

function homeAiDisclosure(titleText, count, open = false, sectionKey = "") {
  const details = el("details", "yunos-ai-disclosure");
  if (sectionKey) details.id = `home-ai-section-${sectionKey}`;
  details.open = open;
  details.appendChild(el(
    "summary",
    "",
    t(
      "shell.extra.sectionWithCount",
      { title: titleText, count: formatUiNumber(count) },
      `${titleText}（${formatUiNumber(count)}）`,
    ),
  ));
  const body = el("div", "yunos-ai-disclosure-content");
  details.appendChild(body);
  return { details, body };
}

function appendHomeAiSources(target, preview) {
  const evidence = homeAiEvidenceItems(preview);
  const { details, body } = homeAiDisclosure(
    t("ai.sources", {}, "来源"),
    evidence.length,
    false,
    "sources",
  );
  if (!evidence.length) {
    body.appendChild(el(
      "p",
      "yunos-ai-empty-supporting",
      t("ai.noTraceableEvidence", {}, "本次 preview 没有可追溯的历史证据。"),
    ));
  }
  evidence.forEach((item, index) => {
    const anchor = item.source_anchor || {};
    const sourceButton = el("button", "yunos-ai-source-button");
    sourceButton.type = "button";
    sourceButton.dataset.sourceConversationId = anchor.conversation_id || "";
    sourceButton.dataset.sourceMessageId = anchor.message_id || "";
    sourceButton.append(
      el(
        "span",
        "yunos-ai-source-title",
        anchor.conversation_title || t(
          "ai.historicalEvidenceIndex",
          { index: formatUiNumber(index + 1) },
          `历史证据 ${formatUiNumber(index + 1)}`,
        ),
      ),
      el(
        "span",
        "yunos-ai-source-excerpt",
        item.excerpt || t("ai.noExcerpt", {}, "无可显示摘录"),
      ),
      el(
        "span",
        "yunos-ai-source-meta",
        t(
          "ai.sourceMeta",
          {
            time: anchor.time || t("date.noTime", {}, "无时间"),
            id: anchor.message_id || t("search.anchorUnavailable", {}, "锚点不可用"),
          },
          `${anchor.time || "无时间"} · 消息 ${anchor.message_id || "锚点不可用"}`,
        ),
      ),
    );
    sourceButton.addEventListener("click", () => openHomeAiSource(item));
    body.appendChild(sourceButton);
  });
  target.appendChild(details);
}

function appendHomeAiMemories(target, preview) {
  const profiles = homeAiProfileItems(preview);
  const { details, body } = homeAiDisclosure(
    t("ai.relatedMemories", {}, "相关记忆"),
    profiles.length,
    false,
    "memories",
  );
  if (!profiles.length) {
    body.appendChild(el(
      "p",
      "yunos-ai-empty-supporting",
      t("ai.noApprovedProfileIncluded", {}, "本次没有纳入已批准画像。"),
    ));
  }
  profiles.forEach((item) => {
    const row = el("div", "yunos-ai-memory-item");
    row.appendChild(el(
      "p",
      "yunos-ai-memory-text",
      item.statement || t("ai.approvedProfileUnavailable", {}, "已批准画像内容不可用"),
    ));
    body.appendChild(row);
  });
  target.appendChild(details);
}

function appendHomeAiUncertainties(target, preview) {
  const uncertainties = homeAiUncertaintyItems(preview);
  const { details, body } = homeAiDisclosure(
    t("ai.uncertainties", {}, "不确定信息"),
    uncertainties.length,
    false,
    "uncertainties",
  );
  if (!uncertainties.length) {
    body.appendChild(el(
      "p",
      "yunos-ai-empty-supporting",
      t("ai.noRuntimeUncertainty", {}, "Runtime 未返回额外的不确定信息。"),
    ));
  }
  uncertainties.forEach((item) => {
    const row = el("div", "yunos-ai-uncertainty-item");
    row.append(
      el(
        "p",
        "yunos-ai-uncertainty-text",
        item.statement || t("ai.unclassifiedUncertainty", {}, "存在未分类的不确定信息。"),
      ),
      el("span", "yunos-ai-source-meta", item.reason_code || "uncertainty"),
    );
    body.appendChild(row);
  });
  target.appendChild(details);
}

function appendHomeAiExecutionDetails(
  target,
  preview,
  envelope = null,
  { recovered = false, executionUncertain = false } = {},
) {
  const result = envelope?.result || envelope || {};
  const binding = preview?.reviewed_execution_binding || {};
  const artifact = result.request_artifact || preview?.request_artifact || {};
  const transport = envelope?.result ? (envelope.transport || {}) : {};
  const exactlyOne = transport.exactly_one_call === true
    ? t("ai.exactlyOneCallYes", {}, "是（本次请求 1 次）")
    : result?.execution?.external_model_calls === true && result.receipt_identity
      ? t("ai.exactlyOneCallReceipt", {}, "是（Runtime receipt 验证）")
      : executionUncertain
        ? t("ai.callStateUnknownAfterSend", {}, "未知（请求已越过发送边界）")
        : t("ai.notCalled", {}, "尚未调用");
  const externalCalls = state.runtimeMock
    ? (executionUncertain
      ? t("ai.mockNetworkUnknown", {}, "0 外部网络；mock adapter 状态未知")
      : t("ai.mockLocalTransport", {}, "0（本地模拟传输）"))
    : result?.execution?.external_model_calls === true
      ? "1"
      : executionUncertain
        ? t("ai.callStateUnknownAfterSend", {}, "未知（请求已越过发送边界）")
        : "0";
  const { details, body } = homeAiDisclosure(
    t("ai.executionDetails", {}, "执行详情"),
    envelope ? 1 : 0,
    false,
    "execution",
  );
  const grid = el("div", "yunos-ai-detail-grid");
  [
    [
      t("common.status", {}, "状态"),
      enumLabel("runtime", result.status || preview?.status || homeAiState.phase),
    ],
    [
      t("ai.recoverySource", {}, "恢复来源"),
      recovered
        ? t("ai.runtimeLedgerReceipt", {}, "Runtime 账本 / 回执")
        : t("ai.currentPage", {}, "当前页面"),
    ],
    [t("ai.reviewedBinding", {}, "已复核绑定"), binding.identity],
    [t("ai.artifact", {}, "产物"), artifact.artifact_identity],
    [t("ai.run", {}, "运行记录"), result.model_run_id || preview?.diagnostic_bindings?.model_run_id],
    [t("ai.receipt", {}, "回执"), result.receipt_identity || t("ai.notGenerated", {}, "尚未生成")],
    [t("ai.exactlyOneCall", {}, "仅调用一次"), exactlyOne],
    [t("ai.autoRetry", {}, "自动重试"), t("common.no", {}, "否")],
    [t("ai.externalNetworkRequests", {}, "外部网络请求"), externalCalls],
    [t("ai.promptTokens", {}, "输入 Token"), result.usage?.prompt_tokens ?? t("ai.notGenerated", {}, "尚未生成")],
    [t("ai.completionTokens", {}, "输出 Token"), result.usage?.completion_tokens ?? t("ai.notGenerated", {}, "尚未生成")],
    [t("ai.totalTokens", {}, "Token 总数"), result.usage?.total_tokens ?? t("ai.notGenerated", {}, "尚未生成")],
    [t("ai.finishReason", {}, "结束原因"), result.response?.finish_reason || t("ai.notGenerated", {}, "尚未生成")],
    [t("ai.delivery", {}, "交付内容"), result.delivery_identity || preview?.delivery_identity],
  ].forEach(([label, value]) => grid.appendChild(homeAiDetailItem(
    label,
    value || t("status.unavailable", {}, "不可用"),
  )));
  body.appendChild(grid);
  target.appendChild(details);
}

function appendHomeAiSupportingSections(target, preview, envelope = null, options = {}) {
  const supporting = el("section", "yunos-ai-supporting-sections");
  appendHomeAiSources(supporting, preview);
  appendHomeAiMemories(supporting, preview);
  appendHomeAiUncertainties(supporting, preview);
  appendHomeAiExecutionDetails(supporting, preview, envelope, options);
  target.appendChild(supporting);
}

function cognitiveLatestRecord(value) {
  const items = Array.isArray(value)
    ? value
    : (Array.isArray(value?.items) ? value.items : []);
  if (!items.length) return null;
  const sorted = [...items].sort((left, right) => {
    const leftTime = String(left?.last_activity_at || left?.updated_at || left?.created_at || "");
    const rightTime = String(right?.last_activity_at || right?.updated_at || right?.created_at || "");
    if (leftTime !== rightTime) return leftTime.localeCompare(rightTime);
    return Number(left?.version || 0) - Number(right?.version || 0);
  });
  return sorted[sorted.length - 1] || null;
}

function cognitiveBundleRoot(bundle = homeAiState.cognitiveBundle) {
  if (!bundle || typeof bundle !== "object") return {};
  const root = bundle.bundle || bundle.workspace || bundle.data || bundle;
  return root && typeof root === "object" ? root : {};
}

function cognitiveCollectionItems(value) {
  if (Array.isArray(value)) return value;
  return Array.isArray(value?.items) ? value.items : [];
}

function validateCognitiveReadonlyEnvelope(payload, expectedSurface = "") {
  if (
    !payload
    || typeof payload !== "object"
    || payload.readonly !== true
    || payload.cache_control !== "no-store"
  ) {
    throw new Error("COGNITIVE_READONLY_ENVELOPE_INVALID");
  }
  if (expectedSurface) {
    const boundaries = payload.boundaries || {};
    if (
      payload.workspace_schema_version !== 1
      || payload.surface !== expectedSurface
      || boundaries.readonly !== true
      || boundaries.write_api_available !== false
      || boundaries.mobile_write_allowed !== false
      || boundaries.tailnet_write_allowed !== false
      || boundaries.acceptance_write_allowed !== false
      || boundaries.authority_expansion_allowed !== false
    ) {
      throw new Error("COGNITIVE_WORKSPACE_BOUNDARY_INVALID");
    }
  }
  return payload;
}

async function loadCognitiveWorkspaceBundle({
  surface = "desktop",
  preferredTitle = "",
  allowLatest = false,
} = {}) {
  if (!['desktop', 'mobile'].includes(surface)) {
    throw new Error("COGNITIVE_WORKSPACE_SURFACE_INVALID");
  }
  const listing = validateCognitiveReadonlyEnvelope(await api(COGNITIVE_RUNS_ENDPOINT));
  const trustedRuns = cognitiveCollectionItems(listing)
    .filter((item) => (
      item
      && typeof item === "object"
      && /^[A-Za-z0-9._:-]{8,128}$/.test(String(item.run_id || ""))
    ));
  const normalizedTitle = String(preferredTitle || "").trim();
  let selected = normalizedTitle
    ? trustedRuns.find((item) => String(item.title || "").trim() === normalizedTitle)
    : null;
  if (!selected && allowLatest && trustedRuns.length) {
    selected = cognitiveLatestRecord(trustedRuns);
  }
  if (!selected) return null;
  const params = new URLSearchParams({
    run_id: selected.run_id,
    surface,
  });
  const workspace = await api(`${COGNITIVE_WORKSPACE_BUNDLE_ENDPOINT}?${params.toString()}`);
  return validateCognitiveReadonlyEnvelope(workspace, surface);
}

function refreshHomeAiCognitiveWorkspace() {
  if (!homeAiDocument?.hasAttribute("data-cognitive-workspace")) return;
  if (
    homeAiState.phase === HOME_AI_PHASES.CANCELLED_OR_UNAVAILABLE
    && homeAiState.runtimeFailure
  ) {
    const failure = homeAiState.runtimeFailure;
    renderCognitiveWorkspace({
      phase: HOME_AI_PHASES.CANCELLED_OR_UNAVAILABLE,
      preview: homeAiState.preview,
      envelope: failure.envelope,
      lead: failure.message,
      agentMessage: failure.message,
      technicalDetail: failure.technicalDetail,
      executionUncertain: failure.executionUncertain,
      actionBuilder: appendCognitiveReturnAction,
    });
    return;
  }
  if ([HOME_AI_PHASES.SUCCESS, HOME_AI_PHASES.TRUNCATED, HOME_AI_PHASES.PROVIDER_ERROR].includes(
    homeAiState.phase,
  ) && homeAiState.preview && homeAiState.envelope) {
    renderHomeAiResult(homeAiState.preview, homeAiState.envelope);
    return;
  }
  if ([HOME_AI_PHASES.PREVIEW_READY, HOME_AI_PHASES.INSUFFICIENT_EVIDENCE].includes(
    homeAiState.phase,
  ) && homeAiState.preview) {
    renderHomeAiPreview(homeAiState.preview);
    return;
  }
  if (homeAiIsWorking(homeAiState.phase)) {
    renderHomeAiPending(homeAiState.phase, homeAiState.preview);
  }
}

async function hydrateHomeAiCognitiveBundle(preferredTitle, requestSerial) {
  homeAiState.cognitiveBundleStatus = "loading";
  homeAiState.cognitiveBundleDetail = "";
  try {
    const bundle = await loadCognitiveWorkspaceBundle({
      surface: "desktop",
      preferredTitle,
      allowLatest: false,
    });
    if (requestSerial !== homeAiState.requestSerial) return;
    homeAiState.cognitiveBundle = bundle;
    homeAiState.cognitiveBundleStatus = bundle ? "ready" : "unavailable";
    homeAiState.cognitiveBundleDetail = bundle ? "" : "COGNITIVE_RUN_NOT_BOUND";
  } catch (error) {
    if (requestSerial !== homeAiState.requestSerial) return;
    homeAiState.cognitiveBundle = null;
    homeAiState.cognitiveBundleStatus = "unavailable";
    homeAiState.cognitiveBundleDetail = error?.technicalDetail
      || error?.message
      || "COGNITIVE_WORKSPACE_UNAVAILABLE";
  }
  refreshHomeAiCognitiveWorkspace();
}

function cognitiveBundleParts(bundle = homeAiState.cognitiveBundle) {
  const root = cognitiveBundleRoot(bundle);
  const objective = root.current_objective
    || root.objective_contract
    || root.objective
    || cognitiveLatestRecord(root.objectives);
  const contract = objective?.contract
    || objective?.document
    || objective?.objective_contract
    || objective
    || {};
  const plan = root.current_plan
    || root.plan_graph
    || root.plan
    || cognitiveLatestRecord(root.plans);
  const run = root.current_run
    || root.cognitive_run
    || root.run
    || cognitiveLatestRecord(root.runs);
  const authorization = root.authorization_artifact
    || root.authorization
    || cognitiveLatestRecord(root.authorizations);
  const provider = root.provider_profile
    || root.provider
    || cognitiveLatestRecord(root.providers);
  const checkpoint = root.checkpoint
    || cognitiveLatestRecord(root.checkpoints);
  const outcome = root.outcome?.current
    || root.outcome
    || cognitiveLatestRecord(root.outcomes);
  const acceptance = root.acceptance_event
    || root.acceptance
    || cognitiveLatestRecord(root.acceptances);
  const verifications = cognitiveCollectionItems(
    root.verification_records || root.verifications,
  );
  const learning = cognitiveCollectionItems(root.learning_candidates || root.learning);
  const supervision = cognitiveCollectionItems(root.supervision_events || root.supervision || root.events);
  return {
    root,
    objective: objective && typeof objective === "object" ? objective : {},
    contract: contract && typeof contract === "object" ? contract : {},
    plan: plan && typeof plan === "object" ? plan : {},
    run: run && typeof run === "object" ? run : {},
    authorization: authorization && typeof authorization === "object" ? authorization : {},
    provider: provider && typeof provider === "object" ? provider : {},
    checkpoint: checkpoint && typeof checkpoint === "object" ? checkpoint : {},
    outcome: outcome && typeof outcome === "object" ? outcome : {},
    acceptance: acceptance && typeof acceptance === "object" ? acceptance : {},
    verifications,
    learning,
    supervision,
  };
}

function cognitiveTextItems(value) {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    if (typeof item === "string") return item.trim();
    if (!item || typeof item !== "object") return "";
    return String(
      item.title
      || item.statement
      || item.description
      || item.path
      || item.capability_id
      || item.reason
      || item.value
      || "",
    ).trim();
  }).filter(Boolean);
}

function cognitivePhaseLabel(phase, runState = "") {
  const normalizedRun = String(runState || "").toLowerCase();
  const runKeys = {
    queued: "queued",
    reasoning: "reasoning",
    planning: "planning",
    waiting_authority: "waitingAuthority",
    executing: "executing",
    waiting_external: "waitingExternal",
    verifying: "verifying",
    waiting_acceptance: "waitingAcceptance",
    completed: "completed",
    failed: "failed",
    cancelled: "cancelled",
    recovery_required: "recoveryRequired",
  };
  if (runKeys[normalizedRun]) {
    return t(
      `cognitive.run.${runKeys[normalizedRun]}`,
      {},
      normalizedRun.replaceAll("_", " "),
    );
  }
  const phaseKeys = {
    [HOME_AI_PHASES.PREPARING_PREVIEW]: ["preparing", "Preparing preview"],
    [HOME_AI_PHASES.PREVIEW_READY]: ["ready", "Waiting for explicit execution"],
    [HOME_AI_PHASES.EXECUTING]: ["executing", "Executing"],
    [HOME_AI_PHASES.RECEIVING_RESULT]: ["receiving", "Waiting for Runtime terminal state"],
    [HOME_AI_PHASES.SUCCESS]: ["success", "Agent reported"],
    [HOME_AI_PHASES.TRUNCATED]: ["truncated", "Outcome truncated"],
    [HOME_AI_PHASES.INSUFFICIENT_EVIDENCE]: ["insufficient", "Insufficient evidence"],
    [HOME_AI_PHASES.PROVIDER_ERROR]: ["failed", "Execution failed"],
    [HOME_AI_PHASES.CANCELLED_OR_UNAVAILABLE]: ["unavailable", "Cancelled or unavailable"],
  };
  const [key, fallback] = phaseKeys[phase] || ["preparing", "Preparing preview"];
  return t(`cognitive.phase.${key}`, {}, fallback);
}

function cognitiveAgencyLabel(value) {
  const normalized = ["consult", "plan", "prepare", "act"].includes(value)
    ? value
    : "consult";
  const fallback = {
    consult: "Consult",
    plan: "Plan",
    prepare: "Prepare",
    act: "Act",
  }[normalized];
  return t(`cognitive.agency.${normalized}`, {}, fallback);
}

function cognitiveStanceLabel(value) {
  const normalized = ["continue", "explore", "breakthrough"].includes(value)
    ? value
    : "continue";
  const fallback = {
    continue: "Continue",
    explore: "Explore",
    breakthrough: "Breakthrough",
  }[normalized];
  return t(`cognitive.stance.${normalized}`, {}, fallback);
}

function cognitiveRiskLabel(value) {
  const raw = String(value || "").trim();
  const normalized = raw.toLowerCase();
  if (["low", "medium", "high", "critical"].includes(normalized)) {
    const fallback = {
      low: "Low",
      medium: "Medium",
      high: "High",
      critical: "Critical",
    }[normalized];
    return t(`cognitive.risk.${normalized}`, {}, fallback);
  }
  if (!raw) return t("cognitive.notRecorded", {}, "Not recorded");
  return t(
    "cognitive.risk.unrecognized",
    { value: raw },
    `Unrecognized risk: ${raw}`,
  );
}

function cognitiveDateLabel(value) {
  if (!value) return t("cognitive.notRecorded", {}, "Not recorded");
  try {
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) throw new Error("invalid_date");
    return formatUiDateTime(parsed, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return String(value);
  }
}

function cognitiveWorkspaceModel(
  preview = homeAiState.preview,
  envelope = homeAiState.envelope,
  phase = homeAiState.phase,
  bundle = homeAiState.cognitiveBundle,
) {
  const parts = cognitiveBundleParts(bundle);
  const result = envelope?.result || envelope || {};
  const runtimeProfile = preview?.runtime_profile || preview?.request_artifact?.runtime_profile || {};
  const contract = parts.contract;
  const objective = parts.objective;
  const plan = parts.plan;
  const run = parts.run;
  const authorization = parts.authorization;
  const evidence = homeAiEvidenceItems(preview);
  const profiles = homeAiProfileItems(preview);
  const question = String(
    contract.user_utterance
    || preview?.question
    || homeAiState.question
    || "",
  ).trim();
  const agencyLevel = ["consult", "plan", "prepare", "act"].includes(contract.agency_level)
    ? contract.agency_level
    : "consult";
  const cognitiveStance = ["continue", "explore", "breakthrough"].includes(contract.cognitive_stance)
    ? contract.cognitive_stance
    : "continue";
  const risk = String(
    contract.risk_tolerance
    || objective.risk_tolerance
    || run.risk_ceiling
    || "",
  ).trim().toLowerCase();
  const providerName = parts.provider.display_name
    || parts.provider.provider_id
    || runtimeProfile.provider
    || t("cognitive.notBound", {}, "Not bound");
  const providerModel = parts.provider.model || runtimeProfile.model || "";
  const explicitAccepted = [
    parts.acceptance.status,
    parts.acceptance.current_status,
    parts.outcome.status,
    parts.outcome.acceptance_status,
  ].some((value) => String(value || "").toLowerCase() === "accepted");
  const runtimeVerified = Boolean(
    ["succeeded", "failed"].includes(result.status)
    && result.receipt_identity
    && result.execution?.persisted === true
  );
  const storedVerified = parts.verifications.some((item) => [
    item?.status,
    item?.result,
    item?.verification_status,
  ].some((value) => ["verified", "passed", "succeeded"].includes(
    String(value || "").toLowerCase(),
  )));
  const overreachEvents = parts.supervision.filter((item) => (
    String(item?.event_type || item?.kind || item?.reason_code || "")
      .toLowerCase()
      .includes("overreach")
  ));
  const startTime = run.started_at || run.created_at || objective.created_at || "";
  const lastActivity = run.last_activity_at
    || run.updated_at
    || parts.outcome.created_at
    || objective.created_at
    || "";
  const duration = startTime && Number.isFinite(Date.parse(startTime))
    ? formatUiDuration(Math.max(0, Date.now() - Date.parse(startTime)))
    : t("cognitive.notRecorded", {}, "Not recorded");
  return {
    parts,
    result,
    preview,
    envelope,
    phase,
    question,
    title: String(
      contract.interpreted_goal
      || question
      || t("cognitive.taskTitleFallback", {}, "Current cognitive task"),
    ),
    project: String(
      parts.root.project?.display_name
      || parts.root.project?.title
      || objective.project_id
      || contract.project_id
      || t("cognitive.notBound", {}, "Not bound"),
    ),
    provider: providerModel ? `${providerName} · ${providerModel}` : String(providerName),
    agencyLevel,
    cognitiveStance,
    risk,
    status: cognitivePhaseLabel(phase, run.state || run.status),
    lastActivity: cognitiveDateLabel(lastActivity),
    lastActivityAt: lastActivity,
    duration,
    objective,
    contract,
    plan,
    run,
    authorization,
    evidence,
    profiles,
    explicitAccepted,
    systemVerified: runtimeVerified || storedVerified,
    overreachEvents,
    needsSupervision: ["prepare", "act"].includes(agencyLevel),
    bundleBound: Boolean(objective.objective_id || contract.objective_id),
  };
}

function cognitiveSection(sectionKey) {
  const section = el("section", "yunos-cognitive-section");
  section.dataset.cognitiveSection = sectionKey;
  section.appendChild(el(
    "h2",
    "yunos-cognitive-section-title",
    t(`cognitive.section.${sectionKey}`, {}, {
      objective: "Objective interpretation",
      success: "Success criteria",
      prohibited: "Explicitly excluded",
      context: "Personal context used",
      plan: "Plan",
      decisions: "Decision points",
      execution: "Current execution",
      verification: "Verification",
      outcome: "Outcome",
      learning: "Learning candidates",
    }[sectionKey] || sectionKey),
  ));
  const body = el("div", "yunos-cognitive-section-body");
  section.appendChild(body);
  return { section, body };
}

function appendCognitiveList(target, items, emptyText = "") {
  const values = cognitiveTextItems(items);
  if (!values.length) {
    if (emptyText) target.appendChild(el("p", "yunos-cognitive-empty", emptyText));
    return;
  }
  const list = el("ul", "yunos-cognitive-list");
  values.forEach((value) => list.appendChild(el("li", "", value)));
  target.appendChild(list);
}

function cognitivePlanFallback(phase) {
  const completedPreview = ![
    HOME_AI_PHASES.IDLE,
    HOME_AI_PHASES.PREPARING_PREVIEW,
  ].includes(phase);
  const calledProvider = [
    HOME_AI_PHASES.SUCCESS,
    HOME_AI_PHASES.TRUNCATED,
    HOME_AI_PHASES.PROVIDER_ERROR,
  ].includes(phase);
  const providerActive = [
    HOME_AI_PHASES.EXECUTING,
    HOME_AI_PHASES.RECEIVING_RESULT,
  ].includes(phase);
  return [
    {
      title: t("cognitive.planContext", {}, "Assemble the minimum necessary context"),
      state: completedPreview ? "completed" : "active",
    },
    {
      title: t("cognitive.planProvider", {}, "Generate an answer with the explicitly selected Provider"),
      state: calledProvider ? "completed" : (providerActive ? "active" : "pending"),
    },
    {
      title: t("cognitive.planVerify", {}, "Verify the Runtime receipt and outcome boundary"),
      state: calledProvider ? "completed" : "pending",
    },
  ];
}

function cognitivePlanStateLabel(value) {
  const normalized = String(value || "pending").toLowerCase();
  const completed = ["completed", "succeeded", "verified"].includes(normalized);
  const active = ["active", "running", "executing", "verifying"].includes(normalized);
  const blocked = normalized.startsWith("waiting") || ["blocked", "human_gate"].includes(normalized);
  const key = completed ? "completed" : (active ? "active" : (blocked ? "blocked" : "pending"));
  const fallback = {
    completed: "Completed",
    active: "Active",
    blocked: "Waiting for gate",
    pending: "Pending",
  }[key];
  return { key, label: t(`cognitive.planState.${key}`, {}, fallback) };
}

function cognitivePlanDisplayState(node, model) {
  const nodeId = String(node?.node_id || "");
  const checkpoint = model?.parts?.checkpoint || {};
  const completedNodeIds = Array.isArray(checkpoint.completed_node_ids)
    ? checkpoint.completed_node_ids
    : [];
  const currentNodeIds = Array.isArray(checkpoint.current_node_ids)
    ? checkpoint.current_node_ids
    : [];
  if (nodeId && completedNodeIds.includes(nodeId)) return "completed";
  if (nodeId && currentNodeIds.includes(nodeId)) return "active";
  return node?.state;
}

function appendCognitivePlan(target, model) {
  const planNodes = Array.isArray(model.plan.nodes) ? model.plan.nodes : [];
  if (!planNodes.length) {
    target.appendChild(el(
      "p",
      "yunos-cognitive-boundary-note",
      t(
        "cognitive.planUnbound",
        {},
        "No persisted Plan Graph is bound. The list below is only the current Runtime stage trace.",
      ),
    ));
  }
  const nodes = planNodes.length ? planNodes : cognitivePlanFallback(model.phase);
  const list = el("ol", "yunos-cognitive-plan-list");
  nodes.forEach((node, index) => {
    const displayState = cognitivePlanDisplayState(node, model);
    const stateInfo = cognitivePlanStateLabel(displayState);
    const item = el("li", "yunos-cognitive-plan-node");
    item.dataset.planState = stateInfo.key;
    if (displayState !== node.state) item.dataset.planStateSource = "checkpoint";
    const heading = el("div", "yunos-cognitive-plan-node-heading");
    heading.append(
      el("span", "yunos-cognitive-plan-index", formatUiNumber(index + 1)),
      el("strong", "", node.title || t("cognitive.notAvailable", {}, "Unavailable")),
      el("span", "yunos-cognitive-plan-state", stateInfo.label),
    );
    item.appendChild(heading);
    if (node.purpose) item.appendChild(el("p", "", node.purpose));
    list.appendChild(item);
  });
  target.appendChild(list);
}

function cognitivePermissionLabel(value) {
  return value === true
    ? t("cognitive.permissionAllowed", {}, "Yes")
    : t("cognitive.permissionDenied", {}, "No");
}

function cognitiveScopeText(value, fallback) {
  const items = cognitiveTextItems(value);
  return items.length ? items.join("\n") : fallback;
}

function cognitiveBudgetText(authority, preview) {
  const values = [];
  if (Number.isFinite(Number(authority.max_model_calls))) {
    values.push(t(
      "cognitive.budgetModelCalls",
      { count: formatUiNumber(authority.max_model_calls) },
      `${formatUiNumber(authority.max_model_calls)} model call(s)`,
    ));
  }
  const maxTokens = authority.max_tokens ?? authority.max_token_budget;
  if (Number.isFinite(Number(maxTokens))) {
    values.push(t(
      "cognitive.budgetTokens",
      { count: formatUiNumber(maxTokens) },
      `${formatUiNumber(maxTokens)} tokens`,
    ));
  }
  if (Number.isFinite(Number(authority.max_cost_microunits))) {
    values.push(t(
      "cognitive.budgetCost",
      { count: formatUiNumber(authority.max_cost_microunits) },
      `${formatUiNumber(authority.max_cost_microunits)} cost microunits`,
    ));
  }
  return values.join(" · ")
    || preview?.budget_profile
    || homeAiState.budget
    || t("cognitive.notRecorded", {}, "Not recorded");
}

function cognitiveStallStatus(model, nowMs = Date.now()) {
  const persistedStall = model.run.suspected_stall === true
    || model.parts.supervision.some((item) => [
      item?.event_type,
      item?.kind,
      item?.reason_code,
    ].some((value) => String(value || "").toLowerCase().includes("suspected_stall")));
  if (persistedStall) {
    return t(
      "cognitive.stallSuspected",
      {},
      "A persisted suspected_stall event requires immediate review.",
    );
  }
  const rawActivity = model.lastActivityAt || model.parts.checkpoint.last_activity_at || "";
  const activityTime = Date.parse(rawActivity);
  const currentTime = Number(nowMs);
  if (!Number.isFinite(activityTime) || !Number.isFinite(currentTime) || activityTime > currentTime) {
    return t(
      "cognitive.stallUnknown",
      {},
      "No verifiable stall-check record is available yet",
    );
  }
  const ageMs = currentTime - activityTime;
  const ageMinutes = Math.floor(ageMs / 60_000);
  if (ageMs >= COGNITIVE_STALL_THRESHOLD_MS) {
    return t(
      "cognitive.stallOverdue",
      { minutes: formatUiNumber(ageMinutes) },
      `${formatUiNumber(ageMinutes)} minutes since the last activity; the 20-minute threshold requires a check, and no persisted check is visible.`,
    );
  }
  return t(
    "cognitive.stallHealthy",
    { minutes: formatUiNumber(ageMinutes) },
    `${formatUiNumber(ageMinutes)} minutes since the last activity; still within the 20-minute threshold.`,
  );
}

function renderCognitiveSupervision(model) {
  const authority = model.authorization;
  const request = model.contract.authority_request || {};
  const aside = el("aside", "yunos-cognitive-supervision");
  aside.dataset.cognitiveSupervision = "";
  aside.appendChild(el(
    "h2",
    "yunos-cognitive-supervision-title",
    t("cognitive.supervisionTitle", {}, "Supervision and authority"),
  ));
  const allowedCapabilities = authority.allowed_capabilities
    || request.allowed_capabilities
    || request.capability_requirements
    || [];
  const blockedCount = model.overreachEvents.length;
  const recentActivity = model.lastActivity;
  const details = [
    [t("cognitive.agent", {}, "Agent"), model.run.agent_name || model.run.executor_id || t("cognitive.nativeAgent", {}, "YunOS native bounded Agent")],
    [t("cognitive.readScope", {}, "Authorized read scope"), cognitiveScopeText(authority.read_roots || request.read_roots, t("cognitive.notBound", {}, "Not bound"))],
    [t("cognitive.writeScope", {}, "Authorized write scope"), cognitiveScopeText(authority.write_roots || request.write_roots, t("cognitive.noWriteScope", {}, "No project write scope"))],
    [t("cognitive.allowedTools", {}, "Allowed tools"), cognitiveScopeText(allowedCapabilities, t("cognitive.noToolExecution", {}, "No tool execution"))],
    [t("cognitive.deniedOperations", {}, "Denied operations"), cognitiveScopeText(authority.denied_capabilities, t("cognitive.defaultDeniedOperations", {}, "Arbitrary shell, out-of-scope writes, secret access, network, merge, tag, push, and next-version creation"))],
    [t("cognitive.commitPermission", {}, "Commit allowed"), cognitivePermissionLabel(authority.commit_permission || authority.commit_allowed)],
    [t("cognitive.mergePermission", {}, "Merge allowed"), cognitivePermissionLabel(authority.merge_permission || authority.merge_allowed)],
    [t("cognitive.tagPermission", {}, "Tag allowed"), cognitivePermissionLabel(authority.tag_permission || authority.tag_allowed)],
    [t("cognitive.pushPermission", {}, "Push allowed"), cognitivePermissionLabel(authority.push_permission || authority.push_allowed)],
    [t("cognitive.runtimeDuration", {}, "Runtime duration"), model.duration],
    [t("cognitive.recentActivity", {}, "Recent activity"), recentActivity],
    [
      t("cognitive.stallCheck", {}, "Stall check"),
      cognitiveStallStatus(model),
    ],
    [t("cognitive.budget", {}, "Budget"), cognitiveBudgetText(authority, model.preview)],
    [
      t("cognitive.overreach", {}, "Overreach status"),
      blockedCount
        ? t("cognitive.overreachBlocked", { count: formatUiNumber(blockedCount) }, `Blocked ${formatUiNumber(blockedCount)} overreach attempt(s)`)
        : t("cognitive.noOverreach", {}, "No overreach detected"),
    ],
  ];
  const list = el("dl", "yunos-cognitive-supervision-list");
  details.forEach(([label, value]) => {
    const row = el("div", "yunos-cognitive-supervision-item");
    row.append(el("dt", "", label), el("dd", "", String(value ?? "—")));
    list.appendChild(row);
  });
  aside.appendChild(list);
  return aside;
}

function appendCognitiveTopSummary(target, model) {
  const summary = el("section", "yunos-cognitive-top-summary");
  [
    [t("cognitive.project", {}, "Project"), model.project],
    [t("cognitive.status", {}, "Status"), model.status],
    [t("cognitive.provider", {}, "Provider"), model.provider],
    [t("cognitive.agencyLevel", {}, "Agency Level"), cognitiveAgencyLabel(model.agencyLevel)],
    [t("cognitive.cognitiveStance", {}, "Cognitive Stance"), cognitiveStanceLabel(model.cognitiveStance)],
    [t("cognitive.risk", {}, "Risk"), cognitiveRiskLabel(model.risk)],
    [t("cognitive.lastActivity", {}, "Last activity"), model.lastActivity],
  ].forEach(([label, value]) => summary.appendChild(homeAiSummaryItem(label, value)));
  target.appendChild(summary);
}

function appendCognitiveObjective(target, model) {
  const { section, body } = cognitiveSection("objective");
  body.appendChild(el(
    "p",
    "yunos-cognitive-primary-copy",
    model.contract.interpreted_goal || model.question || model.title,
  ));
  if (!model.bundleBound) {
    body.appendChild(el(
      "p",
      "yunos-cognitive-boundary-note",
      t(
        "cognitive.objectiveUnbound",
        {},
        "This Runtime task is not bound to a persisted Objective Contract. The workspace shows verified input without presenting it as an approved objective.",
      ),
    ));
  }
  const grid = el("dl", "yunos-cognitive-identity-grid");
  [
    [t("cognitive.objectiveId", {}, "Objective ID"), model.objective.objective_id || model.contract.objective_id || t("cognitive.notBound", {}, "Not bound")],
    [t("cognitive.canonicalDigest", {}, "Canonical digest"), model.objective.canonical_digest || model.contract.canonical_digest || t("cognitive.notRecorded", {}, "Not recorded")],
    [
      t("cognitive.source", {}, "Data source"),
      model.bundleBound
        ? t("cognitive.sourceObjective", {}, "Source: Objective Contract / Plan Graph")
        : t("cognitive.sourceRuntime", {}, "Source: Runtime preview / receipt"),
    ],
  ].forEach(([label, value]) => {
    const item = el("div", "yunos-cognitive-identity-item");
    item.append(el("dt", "", label), el("dd", "", String(value || "—")));
    grid.appendChild(item);
  });
  body.appendChild(grid);
  target.appendChild(section);
}

function appendCognitiveSuccess(target, model) {
  const { section, body } = cognitiveSection("success");
  const criteria = cognitiveTextItems(model.contract.success_criteria);
  appendCognitiveList(
    body,
    criteria.length ? criteria : [t(
      "cognitive.successFallback",
      {},
      "Produce a source-traceable answer with explicit boundaries and visible uncertainty.",
    )],
  );
  target.appendChild(section);
}

function appendCognitiveProhibited(target, model) {
  const { section, body } = cognitiveSection("prohibited");
  const prohibited = cognitiveTextItems(model.contract.prohibited_outcomes);
  appendCognitiveList(body, prohibited.length ? prohibited : [
    t("cognitive.prohibitedProjectWrite", {}, "Do not modify the production project or operational state."),
    t("cognitive.prohibitedSelfAcceptance", {}, "Do not treat an Agent report or Runtime success as user acceptance."),
    t("cognitive.prohibitedProfileWrite", {}, "Do not change the long-term profile from a single task."),
  ]);
  target.appendChild(section);
}

function appendCognitiveContext(target, model) {
  const { section, body } = cognitiveSection("context");
  const bindings = cognitiveCollectionItems(model.parts.root.context);
  body.appendChild(el(
    "p",
    "yunos-cognitive-section-intro",
    t(
      "cognitive.contextCounts",
      {
        evidence: formatUiNumber(model.evidence.length),
        profile: formatUiNumber(model.profiles.length),
      },
      `Included ${formatUiNumber(model.evidence.length)} historical evidence items and ${formatUiNumber(model.profiles.length)} approved profile items.`,
    ),
  ));
  if (bindings.length) {
    appendCognitiveList(body, bindings.map((item) => ({
      title: `${item.source_system || "context"} · ${formatUiNumber(item.selected_items || 0)}/${formatUiNumber(item.maximum_items || 0)}`,
    })));
  }
  if (model.preview) {
    appendHomeAiSources(body, model.preview);
    appendHomeAiMemories(body, model.preview);
  }
  target.appendChild(section);
}

function appendCognitivePlanSection(target, model) {
  const { section, body } = cognitiveSection("plan");
  if (model.plan.plan_id) {
    const meta = el("p", "yunos-cognitive-section-intro");
    meta.textContent = t(
      "cognitive.planNodes",
      { count: formatUiNumber(Array.isArray(model.plan.nodes) ? model.plan.nodes.length : 0) },
      `${formatUiNumber(Array.isArray(model.plan.nodes) ? model.plan.nodes.length : 0)} verifiable nodes`,
    );
    meta.title = `${t("cognitive.planId", {}, "Plan ID")}: ${model.plan.plan_id}`;
    body.appendChild(meta);
  }
  appendCognitivePlan(body, model);
  target.appendChild(section);
}

function appendCognitiveDecisions(target, model) {
  const { section, body } = cognitiveSection("decisions");
  const ambiguities = cognitiveTextItems(model.contract.ambiguities);
  body.appendChild(el(
    "p",
    "yunos-cognitive-section-intro",
    model.contract.clarification_required === true
      ? t(
        "cognitive.decisionClarification",
        {},
        "The objective contains a decision that requires user clarification. High-impact answers must not be guessed.",
      )
      : t(
        "cognitive.decisionNoClarification",
        {},
        "No clarification currently blocks this discussion.",
      ),
  ));
  appendCognitiveList(body, ambiguities);
  if (model.preview) appendHomeAiUncertainties(body, model.preview);
  target.appendChild(section);
}

function appendCognitiveExecution(target, model, technicalDetail = "") {
  const { section, body } = cognitiveSection("execution");
  const executionItems = cognitiveCollectionItems(model.parts.root.executions);
  body.appendChild(el(
    "p",
    "yunos-cognitive-section-intro",
    [
      HOME_AI_PHASES.PREPARING_PREVIEW,
      HOME_AI_PHASES.PREVIEW_READY,
      HOME_AI_PHASES.INSUFFICIENT_EVIDENCE,
    ].includes(model.phase)
      ? t(
        "cognitive.executionPreviewOnly",
        {},
        "Only a Runtime preview is being prepared; the Provider has not been called.",
      )
      : t(
        "cognitive.executionOneCall",
        {},
        "Only one confirmed Provider call is allowed; automatic retry is disabled.",
      ),
  ));
  if (executionItems.length) {
    appendCognitiveList(body, executionItems.map((item) => ({
      title: `${item.capability_id || item.execution_id || "tool"} · ${item.status || "recorded"}`,
    })));
  } else if (!model.preview) {
    body.appendChild(el(
      "p",
      "yunos-cognitive-empty",
      t("cognitive.noToolExecution", {}, "No tool execution"),
    ));
  }
  if (model.preview) {
    appendHomeAiExecutionDetails(body, model.preview, model.envelope, {
      executionUncertain: model.phase === HOME_AI_PHASES.CANCELLED_OR_UNAVAILABLE
        && homeAiState.executionAttempted,
    });
  }
  appendHomeAiTechnicalDetails(body, technicalDetail);
  target.appendChild(section);
}

function appendCognitiveVerification(target, model) {
  const { section, body } = cognitiveSection("verification");
  const records = model.parts.verifications.map((item) => ({
    title: item.statement
      || item.summary
      || `${item.verifier_kind || "system"} · ${item.result || item.status || "recorded"}`,
  }));
  if (records.length) appendCognitiveList(body, records);
  appendCognitiveList(body, [model.systemVerified
    ? t(
      "cognitive.verificationReceipt",
      {},
      "The Runtime terminal state, artifact, and receipt identities passed page-level verification.",
    )
    : model.preview
      ? t(
        "cognitive.verificationPreview",
        {},
        "The Runtime preview identity binding passed page-level verification.",
      )
      : t(
        "cognitive.verificationPending",
        {},
        "System verification is incomplete, so the outcome cannot be marked accepted.",
      )]);
  target.appendChild(section);
}

function cognitiveOutcomeAgentReport(model, overrideMessage = "") {
  if (overrideMessage) return overrideMessage;
  if (model.result.response?.content) return model.result.response.content;
  const storedOutcome = model.parts.outcome.outcome || model.parts.outcome;
  return storedOutcome.agent_report
    || storedOutcome.summary
    || storedOutcome.description
    || model.status;
}

function appendCognitiveOutcome(
  target,
  model,
  {
    agentMessage = "",
    actionBuilder = null,
    executionUncertain = false,
  } = {},
) {
  const { section, body } = cognitiveSection("outcome");
  const unresolved = model.phase === HOME_AI_PHASES.TRUNCATED
    ? t("cognitive.unresolvedTruncated", {}, "The response was truncated; the system will not continue automatically.")
    : [HOME_AI_PHASES.PROVIDER_ERROR, HOME_AI_PHASES.CANCELLED_OR_UNAVAILABLE].includes(model.phase)
      ? t("cognitive.unresolvedFailure", {}, "The Provider or Runtime failed; the failure was not presented as success.")
      : executionUncertain
        ? t("cognitive.systemVerifiedNo", {}, "System verification is incomplete")
        : t("cognitive.noUnresolved", {}, "No known unresolved issues.");
  const storedImpact = model.parts.outcome.outcome?.impact_scope
    || model.parts.outcome.impact_scope;
  const storedRollback = model.parts.outcome.outcome?.rollback
    || model.parts.outcome.rollback;
  const cards = [
    [t("cognitive.agentReport", {}, "Agent report"), cognitiveOutcomeAgentReport(model, agentMessage), "agent"],
    [
      t("cognitive.systemVerification", {}, "System verification"),
      model.systemVerified
        ? t("cognitive.systemVerifiedYes", {}, "Runtime terminal state and identity binding verified")
        : t("cognitive.systemVerifiedNo", {}, "System verification is incomplete"),
      model.systemVerified ? "verified" : "pending",
    ],
    [t("cognitive.unresolvedIssues", {}, "Unresolved issues"), unresolved, "unresolved"],
    [
      t("cognitive.impactScope", {}, "Impact scope"),
      storedImpact || t("cognitive.impactDiscussionOnly", {}, "Produces only a discussion document and Runtime audit record; the project is unchanged."),
      "impact",
    ],
    [
      t("cognitive.rollback", {}, "Rollback"),
      storedRollback || t("cognitive.rollbackNoProjectChange", {}, "No project change needs rollback; return home to end this view."),
      "rollback",
    ],
    [
      t("cognitive.currentDecision", {}, "Current decision"),
      model.systemVerified
        ? t("cognitive.outcomeVerified", {}, "The Runtime outcome is system-verified, but user acceptance was not recorded automatically.")
        : t("cognitive.outcomePending", {}, "An Agent report is not task completion; system verification or the acceptance policy is still required."),
      "decision",
    ],
    [
      t("cognitive.acceptanceState", {}, "Acceptance state"),
      model.explicitAccepted
        ? t("cognitive.acceptedExplicitly", {}, "Accepted by an independent Acceptance Event")
        : t("cognitive.acceptanceNotRecorded", {}, "User acceptance not recorded"),
      model.explicitAccepted ? "accepted" : "waiting",
    ],
  ];
  const grid = el("div", "yunos-cognitive-outcome-grid");
  cards.forEach(([label, value, key]) => {
    const card = el("article", "yunos-cognitive-outcome-card");
    card.dataset.outcomeField = key;
    card.appendChild(el("h3", "", label));
    if (key === "agent") {
      const resultBody = el("section", "yunos-ai-document-body");
      resultBody.appendChild(el(
        "div",
        "yunos-ai-answer",
        String(value || t("cognitive.notRecorded", {}, "Not recorded")),
      ));
      card.appendChild(resultBody);
    } else {
      card.appendChild(el(
        "p",
        "",
        String(value || t("cognitive.notRecorded", {}, "Not recorded")),
      ));
    }
    grid.appendChild(card);
  });
  body.appendChild(grid);
  if (typeof actionBuilder === "function") actionBuilder(body);
  target.appendChild(section);
}

function appendCognitiveLearning(target, model) {
  const { section, body } = cognitiveSection("learning");
  if (model.parts.learning.length) {
    appendCognitiveList(body, model.parts.learning.map((item) => ({
      title: `${item.candidate_type || "candidate"} · ${item.status || "pending"}`,
    })));
  } else {
    body.appendChild(el(
      "p",
      "yunos-cognitive-section-intro",
      t(
        "cognitive.learningNone",
        {},
        "No learning candidate has been created. One outcome cannot directly change long-term preferences or profile data.",
      ),
    ));
  }
  body.appendChild(el(
    "p",
    "yunos-cognitive-boundary-note",
    t(
      "cognitive.learningBoundary",
      {},
      "Only facts, observations, and workflow candidates are eligible; long-term memory still requires independent evidence and approval.",
    ),
  ));
  target.appendChild(section);
}

function renderCognitiveWorkspace({
  phase = homeAiState.phase,
  preview = homeAiState.preview,
  envelope = homeAiState.envelope,
  lead = "",
  agentMessage = "",
  technicalDetail = "",
  actionBuilder = null,
  executionUncertain = false,
} = {}) {
  if (!homeAiDocumentContent) return;
  const model = cognitiveWorkspaceModel(preview, envelope, phase);
  homeAiDocumentContent.replaceChildren();
  const root = el("div", "yunos-cognitive-workspace-document");
  root.appendChild(homeAiHeader(
    t("cognitive.workspaceKicker", {}, "YunOS · Bounded Cognitive Agency"),
    model.title,
    lead,
  ));
  appendCognitiveTopSummary(root, model);
  const bundleStatus = homeAiState.cognitiveBundleStatus;
  root.appendChild(el(
    "p",
    bundleStatus === "unavailable"
      ? "yunos-cognitive-source-note is-fallback"
      : "yunos-cognitive-source-note",
    bundleStatus === "ready"
      ? t("cognitive.bundleReadonly", {}, "Cognitive state comes from a read-only no-store API.")
      : bundleStatus === "loading"
        ? t("cognitive.bundleLoading", {}, "Loading the authoritative read-only cognitive state…")
        : t("cognitive.bundleFallback", {}, "The cognitive state API is unavailable; current verified Runtime data is shown as a fallback."),
  ));
  const layout = el(
    "div",
    model.needsSupervision
      ? "yunos-cognitive-layout has-supervision"
      : "yunos-cognitive-layout",
  );
  const documentColumn = el("main", "yunos-cognitive-document-column");
  appendCognitiveObjective(documentColumn, model);
  appendCognitiveSuccess(documentColumn, model);
  appendCognitiveProhibited(documentColumn, model);
  appendCognitiveContext(documentColumn, model);
  appendCognitivePlanSection(documentColumn, model);
  appendCognitiveDecisions(documentColumn, model);
  appendCognitiveExecution(documentColumn, model, technicalDetail);
  appendCognitiveVerification(documentColumn, model);
  appendCognitiveOutcome(documentColumn, model, {
    agentMessage,
    actionBuilder,
    executionUncertain,
  });
  appendCognitiveLearning(documentColumn, model);
  layout.appendChild(documentColumn);
  if (model.needsSupervision) layout.appendChild(renderCognitiveSupervision(model));
  root.appendChild(layout);
  homeAiDocumentContent.appendChild(root);
}

function appendCognitivePreviewActions(target, preview) {
  const evidence = homeAiEvidenceItems(preview);
  const actions = el("div", "yunos-ai-document-actions");
  const executeButton = el(
    "button",
    "yunos-ai-action primary",
    t("ai.executeOnce", {}, "Execute once"),
  );
  executeButton.id = "home-ai-execute";
  executeButton.type = "button";
  const sidecarReady = preview.runtime_sidecar?.ready === true;
  const executionAvailable = Boolean(
    evidence.length
    && state.canExecute
    && !state.runtimeReview
    && sidecarReady
    && !homeAiState.executionAttempted
  );
  executeButton.disabled = !executionAvailable;
  executeButton.addEventListener("click", executeHomeAiDocumentOnce);
  const cancelButton = el("button", "yunos-ai-action", t("common.cancel", {}, "Cancel"));
  cancelButton.id = "home-ai-cancel";
  cancelButton.type = "button";
  cancelButton.addEventListener("click", cancelHomeAiDocument);
  const note = el(
    "p",
    "yunos-ai-execution-note",
    executionAvailable
      ? (state.runtimeMock
        ? t(
          "ai.mockExecutionNote",
          {},
          "Local acceptance mode: this writes only to a temporary Runtime ledger and sends no network request.",
        )
        : t(
          "ai.executionConfirmationNote",
          {},
          "This confirms exactly one Provider call; automatic retry is disabled.",
        ))
      : !evidence.length
        ? t("ai.executionDisabledNoEvidence", {}, "Execution remains disabled because no historical evidence is available.")
        : state.runtimeReview
          ? t("ai.executionDisabledReviewOnly", {}, "This review-only service cannot call the model.")
          : !state.canExecute
            ? t("ai.executionDisabledNoAuthority", {}, "This service has no execution authority; preview remains read-only.")
            : !sidecarReady
              ? t("ai.executionDisabledRuntimeNotReady", {}, "The Runtime sidecar is not ready; execution remains disabled.")
              : t("ai.executionAlreadySubmitted", {}, "This task has already crossed the execution boundary and cannot be submitted again."),
  );
  actions.append(executeButton, cancelButton, note);
  target.appendChild(actions);
}

function appendCognitiveReturnAction(target) {
  const actions = el("div", "yunos-ai-document-actions");
  const homeButton = el(
    "button",
    "yunos-ai-action",
    t("common.returnHome", {}, "Return home"),
  );
  homeButton.id = "home-ai-return-home";
  homeButton.type = "button";
  homeButton.addEventListener("click", returnToHomeAiIdle);
  actions.appendChild(homeButton);
  target.appendChild(actions);
}

function renderHomeAiPending(phase, preview = null) {
  if (!homeAiDocumentContent) return;
  const isPreparing = phase === HOME_AI_PHASES.PREPARING_PREVIEW;
  const isExecuting = phase === HOME_AI_PHASES.EXECUTING;
  const heading = isPreparing
    ? t("ai.preparingPreview", {}, "正在准备执行预览")
    : isExecuting
      ? t("ai.executingConfirmedRequest", {}, "正在执行已确认的请求")
      : t("ai.receivingRuntimeResult", {}, "正在接收 Runtime 结果");
  const lead = isPreparing
    ? t(
      "ai.preparingPreviewBody",
      {},
      "系统正在只读构建 Context Assembly、Reviewed Binding 与 D1 artifact；此阶段不会调用模型。",
    )
    : isExecuting
      ? t(
        "ai.executingBody",
        {},
        "执行锁已生效，系统只会提交这一份已确认 Binding；自动重试已关闭。",
      )
      : t(
        "ai.receivingBody",
        {},
        "请求已越过发送边界，页面正在等待 Runtime ledger 返回终态；不会自动续写或重试。",
      );
  if (homeAiDocument?.hasAttribute("data-cognitive-workspace")) {
    renderCognitiveWorkspace({
      phase,
      preview,
      lead: `${heading}。${lead}`,
    });
    return;
  }
  homeAiDocumentContent.replaceChildren();
  homeAiDocumentContent.appendChild(homeAiHeader(
    t("ai.document", {}, "AI 文档"),
    heading,
    lead,
  ));
  if (preview) appendHomeAiExecutionSummary(homeAiDocumentContent, preview);
}

function renderHomeAiPreview(preview) {
  if (!homeAiDocumentContent) return;
  const evidence = homeAiEvidenceItems(preview);
  if (homeAiDocument?.hasAttribute("data-cognitive-workspace")) {
    renderCognitiveWorkspace({
      phase: homeAiState.phase,
      preview,
      lead: evidence.length
        ? t(
          "ai.confirmBeforeExecutionBody",
          {},
          "Review the memory scope, evidence, profile, model, and token estimate. Runtime execution starts only after explicit confirmation.",
        )
        : t(
          "ai.insufficientEvidenceBody",
          {},
          "No historical evidence was returned. YunOS will not present an unsupported personal-memory answer as executable.",
        ),
      actionBuilder: (target) => appendCognitivePreviewActions(target, preview),
    });
    return;
  }
  homeAiDocumentContent.replaceChildren();
  homeAiDocumentContent.appendChild(homeAiHeader(
    t("ai.executionPreview", {}, "执行预览"),
    evidence.length
      ? t("ai.confirmBeforeExecution", {}, "执行前确认")
      : t("ai.insufficientEvidence", {}, "证据不足"),
    evidence.length
      ? t(
        "ai.confirmBeforeExecutionBody",
        {},
        "请核对本次记忆范围、证据、画像、模型与 token 估算。只有明确点击“执行一次”才会进入 Runtime 执行链。",
      )
      : t(
        "ai.insufficientEvidenceBody",
        {},
        "本次 Context Assembly 没有返回历史证据。YunOS 不会在无证据时伪装成可执行的个人记忆答案。",
      ),
  ));
  appendHomeAiExecutionSummary(homeAiDocumentContent, preview);

  const actions = el("div", "yunos-ai-document-actions");
  const executeButton = el(
    "button",
    "yunos-ai-action primary",
    t("ai.executeOnce", {}, "执行一次"),
  );
  executeButton.id = "home-ai-execute";
  executeButton.type = "button";
  const sidecarReady = preview.runtime_sidecar?.ready === true;
  const executionAvailable = Boolean(
    evidence.length
    && state.canExecute
    && !state.runtimeReview
    && sidecarReady
    && !homeAiState.executionAttempted
  );
  executeButton.disabled = !executionAvailable;
  executeButton.addEventListener("click", executeHomeAiDocumentOnce);
  const cancelButton = el("button", "yunos-ai-action", t("common.cancel", {}, "取消"));
  cancelButton.id = "home-ai-cancel";
  cancelButton.type = "button";
  cancelButton.addEventListener("click", cancelHomeAiDocument);
  const note = el(
    "p",
    "yunos-ai-execution-note",
    executionAvailable
      ? (state.runtimeMock
        ? t(
          "ai.mockExecutionNote",
          {},
          "本机验收模式：点击后只写入临时 Runtime ledger，不读取凭据、不发出网络请求。",
        )
        : t(
          "ai.executionConfirmationNote",
          {},
          "点击即确认 exactly one provider call；无自动重试。",
        ))
      : !evidence.length
        ? t("ai.executionDisabledNoEvidence", {}, "没有历史证据，执行保持禁用。")
        : state.runtimeReview
          ? t(
            "ai.executionDisabledReviewOnly",
            {},
            "当前为 review-only 服务，preview 保持只读且不会调用模型。",
          )
          : !state.canExecute
            ? t(
              "ai.executionDisabledNoAuthority",
              {},
              "当前服务没有可用执行 authority，preview 保持只读。",
            )
            : !sidecarReady
              ? t(
                "ai.executionDisabledRuntimeNotReady",
                {},
                "Runtime sidecar 未就绪，执行保持禁用且不会绕过 authority。",
              )
              : t(
                "ai.executionAlreadySubmitted",
                {},
                "本次任务已经越过执行确认边界，不能再次提交。",
              ),
  );
  actions.append(executeButton, cancelButton, note);
  homeAiDocumentContent.appendChild(actions);
  appendHomeAiSupportingSections(homeAiDocumentContent, preview);
}

function appendHomeAiTechnicalDetails(target, detail) {
  const technicalDetail = String(detail || "").trim();
  if (!target || !technicalDetail) return;
  const disclosure = document.createElement("details");
  disclosure.className = "yunos-ai-disclosure";
  const body = el("div", "yunos-ai-disclosure-content");
  body.appendChild(el("code", "", technicalDetail));
  disclosure.append(
    el("summary", "", t("common.technicalDetails", {}, "技术详情")),
    body,
  );
  target.appendChild(disclosure);
}

function renderHomeAiResult(preview, envelope, { recovered = false } = {}) {
  if (!homeAiDocumentContent) return;
  const result = envelope?.result || envelope || {};
  const succeeded = result.status === "succeeded";
  const truncated = succeeded && result.response?.finish_reason === "length";
  const phase = succeeded
    ? (truncated ? HOME_AI_PHASES.TRUNCATED : HOME_AI_PHASES.SUCCESS)
    : HOME_AI_PHASES.PROVIDER_ERROR;
  setHomeAiPhase(
    phase,
    recovered
      ? t("ai.resultRecovered", {}, "已从 Runtime ledger 与 receipt 恢复结果。")
      : t("ai.runtimeTerminalReturned", {}, "Runtime 已返回终态。"),
  );
  if (homeAiDocument?.hasAttribute("data-cognitive-workspace")) {
    const truncatedWarning = t(
      "ai.answerTruncated",
      {},
      "回答可能被截断（finish_reason=length）。YunOS 不会自动续写或自动重试。",
    );
    const technicalDetail = succeeded
      ? ""
      : t(
        "error.technicalCategoryCode",
        {
          category: result.error?.category || "provider",
          code: result.error?.code || "unknown",
        },
        `Error category: ${result.error?.category || "provider"}; error code: ${result.error?.code || "unknown"}.`,
      );
    renderCognitiveWorkspace({
      phase,
      preview,
      envelope,
      lead: succeeded
        ? (truncated
          ? truncatedWarning
          : t(
            "ai.resultProvenanceBody",
            {},
            "This content comes from the existing Runtime artifact and immutable receipt; its sources and execution details remain traceable.",
          ))
        : t(
          "ai.failureTruthBody",
          {},
          "YunOS preserves the actual failure state and does not present a Provider error as success.",
        ),
      technicalDetail,
      actionBuilder: appendCognitiveReturnAction,
    });
    return;
  }
  homeAiDocumentContent.replaceChildren();
  homeAiDocumentContent.appendChild(homeAiHeader(
    succeeded
      ? t("ai.documentResult", {}, "AI 文档结果")
      : t("ai.executionError", {}, "AI 执行错误"),
    succeeded
      ? t("ai.conclusionAndBody", {}, "结论与正文")
      : t("ai.executionFailed", {}, "执行未成功"),
    succeeded
      ? t(
        "ai.resultProvenanceBody",
        {},
        "以下内容来自现有 Runtime artifact 与不可变 receipt；来源和执行详情可继续追溯。",
      )
      : t(
        "ai.failureTruthBody",
        {},
        "YunOS 保留真实失败状态，不会把 provider 错误包装成成功结果。",
      ),
  ));

  const body = el("section", "yunos-ai-document-body");
  body.appendChild(el(
    "div",
    "yunos-ai-answer",
    succeeded
      ? (result.response?.content || t(
        "ai.successWithoutBody",
        {},
        "Runtime 成功，但没有返回可显示的正文。",
      ))
      : t("ai.executionFailed", {}, "执行未成功"),
  ));
  if (truncated) {
    body.appendChild(el(
      "p",
      "yunos-ai-state-notice is-warning",
      t(
        "ai.answerTruncated",
        {},
        "回答可能被截断（finish_reason=length）。YunOS 不会自动续写或自动重试。",
      ),
    ));
  }
  if (!succeeded) {
    appendHomeAiTechnicalDetails(
      body,
      t(
        "error.technicalCategoryCode",
        {
          category: result.error?.category || "provider",
          code: result.error?.code || "unknown",
        },
        `错误类别：${result.error?.category || "provider"}；错误代码：${result.error?.code || "unknown"}。`,
      ),
    );
  }
  homeAiDocumentContent.appendChild(body);
  appendHomeAiSupportingSections(homeAiDocumentContent, preview, envelope, { recovered });

  const actions = el("div", "yunos-ai-document-actions");
  const homeButton = el(
    "button",
    "yunos-ai-action",
    t("common.returnHome", {}, "返回首页"),
  );
  homeButton.id = "home-ai-return-home";
  homeButton.type = "button";
  homeButton.addEventListener("click", returnToHomeAiIdle);
  actions.appendChild(homeButton);
  homeAiDocumentContent.appendChild(actions);
}

function renderHomeAiUnavailable(
  message,
  {
    cancelled = false,
    executionUncertain = false,
    envelope = null,
    technicalDetail = "",
  } = {},
) {
  if (!homeAiDocumentContent) return;
  homeAiState.runtimeFailure = {
    message,
    cancelled: Boolean(cancelled),
    executionUncertain: Boolean(executionUncertain),
    envelope,
    technicalDetail: String(technicalDetail || ""),
  };
  setHomeAiPhase(
    HOME_AI_PHASES.CANCELLED_OR_UNAVAILABLE,
    cancelled
      ? t("ai.cancelledNoExternalCall", {}, "任务已取消；没有调用外部模型。")
      : t("ai.runtimeUnavailableNoBypass", {}, "Runtime 当前不可用；不会绕过 authority。"),
  );
  if (homeAiDocument?.hasAttribute("data-cognitive-workspace")) {
    renderCognitiveWorkspace({
      phase: HOME_AI_PHASES.CANCELLED_OR_UNAVAILABLE,
      preview: homeAiState.preview,
      envelope,
      lead: message,
      agentMessage: message,
      technicalDetail,
      executionUncertain,
      actionBuilder: appendCognitiveReturnAction,
    });
    return;
  }
  homeAiDocumentContent.replaceChildren();
  homeAiDocumentContent.appendChild(homeAiHeader(
    cancelled
      ? t("ai.taskCancelled", {}, "AI 任务已取消")
      : t("ai.taskUnavailable", {}, "AI 任务不可用"),
    cancelled
      ? t("status.cancelled", {}, "任务已取消")
      : t("ai.cannotCompleteExecution", {}, "当前无法完成执行"),
    message,
  ));
  appendHomeAiTechnicalDetails(homeAiDocumentContent, technicalDetail);
  if (homeAiState.preview) {
    const notice = el(
      "p",
      cancelled ? "yunos-ai-state-notice" : "yunos-ai-state-notice is-error",
      cancelled
        ? t(
          "ai.cancelledBeforePost",
          {},
          "取消发生在执行 POST 之前，preview 没有读取凭据或调用 provider。",
        )
        : executionUncertain
          ? t(
            "ai.uncertainAfterSend",
            {},
            "请求已越过发送边界，provider 调用状态未知。YunOS 不会自动重试，也不会把未知状态标记为成功。",
          )
          : t(
            "ai.noRetryOnUncertain",
            {},
            "YunOS 不会自动重试，也不会把不确定的执行状态标记为成功。",
          ),
    );
    homeAiDocumentContent.appendChild(notice);
    appendHomeAiSupportingSections(homeAiDocumentContent, homeAiState.preview, envelope, {
      executionUncertain,
    });
  }
  const actions = el("div", "yunos-ai-document-actions");
  const homeButton = el(
    "button",
    "yunos-ai-action",
    t("common.returnHome", {}, "返回首页"),
  );
  homeButton.id = "home-ai-return-home";
  homeButton.type = "button";
  homeButton.addEventListener("click", returnToHomeAiIdle);
  actions.appendChild(homeButton);
  homeAiDocumentContent.appendChild(actions);
}

function homeAiPreviewParams({ question, budget, requestId, mutationId }) {
  return new URLSearchParams({
    q: question,
    budget,
    request_id: requestId,
    mutation_id: mutationId,
  });
}

function validateHomeAiPreviewIdentity(preview, expectedModelRunId = "") {
  const diagnostic = preview?.diagnostic_bindings || {};
  const binding = preview?.reviewed_execution_binding || {};
  const artifact = preview?.request_artifact || {};
  return Boolean(
    preview?.status === "preview"
    && preview.question === homeAiState.question
    && preview.budget_profile === homeAiState.budget
    && preview.request_id === homeAiState.requestId
    && preview.mutation_id === homeAiState.mutationId
    && binding.request_id === homeAiState.requestId
    && binding.mutation_id === homeAiState.mutationId
    && diagnostic.model_run_id
    && (!expectedModelRunId || diagnostic.model_run_id === expectedModelRunId)
    && diagnostic.artifact_identity === artifact.artifact_identity
    && diagnostic.delivery_identity === preview.delivery_identity
    && diagnostic.wire_body_sha256 === artifact.wire_body_sha256
  );
}

function sameHomeAiIdentityList(left, right) {
  return Array.isArray(left)
    && Array.isArray(right)
    && left.length === right.length
    && left.every((value, index) => value === right[index]);
}

function validateHomeAiTerminalResult(result) {
  if (
    !["succeeded", "failed"].includes(result?.status)
    || !/^rrc_[0-9a-f]{64}$/.test(result.receipt_identity || "")
    || result.execution?.persisted !== true
    || typeof result.execution?.external_model_calls !== "boolean"
    || typeof result.execution?.sent_to_model !== "boolean"
  ) return false;
  if (result.status === "succeeded") {
    const usage = result.usage || {};
    return Boolean(
      result.error === null
      && typeof result.response?.content === "string"
      && typeof result.response?.finish_reason === "string"
      && result.response.finish_reason
      && Number.isInteger(usage.prompt_tokens)
      && usage.prompt_tokens >= 0
      && Number.isInteger(usage.completion_tokens)
      && usage.completion_tokens >= 0
      && usage.total_tokens === usage.prompt_tokens + usage.completion_tokens
      && result.execution.sent_to_model === true
      && result.execution.external_model_calls === true
    );
  }
  return Boolean(
    result.response === null
    && result.usage === null
    && typeof result.error?.category === "string"
    && result.error.category
    && typeof result.error?.code === "string"
    && result.error.code
  );
}

function validateHomeAiExecutionEnvelope(envelope, preview, saved) {
  const result = envelope?.result;
  const transport = envelope?.transport || {};
  const externalCalls = result?.execution?.external_model_calls;
  return Boolean(
    validateHomeAiTerminalResult(result)
    && envelope.status === result.status
    && envelope.execution_phase === result.execution_phase
    && transport.automatic_retry === false
    && transport.exactly_one_call === externalCalls
    && transport.model_call_count_for_run === (externalCalls ? 1 : 0)
    && validateHomeAiRecoveredResult(preview, result, saved)
  );
}

function validateHomeAiRecoveredResult(preview, result, saved) {
  const previewArtifact = preview?.request_artifact || {};
  const resultArtifact = result?.request_artifact || {};
  const previewDelivery = previewArtifact.delivery_binding || {};
  const resultDelivery = resultArtifact.delivery_binding || {};
  return Boolean(
    validateHomeAiPreviewIdentity(preview, saved.modelRunId)
    && result?.model_run_id === saved.modelRunId
    && result.request_id === saved.requestId
    && result.mutation_id === saved.mutationId
    && result.delivery_identity === preview.delivery_identity
    && resultArtifact.artifact_identity === previewArtifact.artifact_identity
    && resultArtifact.wire_body_sha256 === previewArtifact.wire_body_sha256
    && resultDelivery.delivery_identity === previewDelivery.delivery_identity
    && resultDelivery.assembly_identity === previewDelivery.assembly_identity
    && resultDelivery.model_context_sha256 === previewDelivery.model_context_sha256
    && sameHomeAiIdentityList(resultDelivery.profile_item_ids, previewDelivery.profile_item_ids)
    && sameHomeAiIdentityList(
      resultDelivery.evidence_reference_ids,
      previewDelivery.evidence_reference_ids,
    )
  );
}

async function startHomeAiPreview(question) {
  clearHomeAiReceivingTimer();
  const requestSerial = ++homeAiState.requestSerial;
  const normalized = normalizeRuntimeQuestion(question);
  const requestId = runtimeIdentifier("request");
  const mutationId = runtimeIdentifier("mutation");
  Object.assign(homeAiState, {
    question: normalized,
    budget: HOME_AI_BUDGET_PROFILE,
    requestId,
    mutationId,
    modelRunId: "",
    preview: null,
    envelope: null,
    executionAttempted: false,
    inFlight: false,
    cognitiveBundle: null,
    cognitiveBundleStatus: "loading",
    cognitiveBundleDetail: "",
    runtimeFailure: null,
  });
  clearHomeAiSession();
  closeHomeMemorySearchOverlay({ invalidateRequest: true });
  if (homeOmniboxInput) homeOmniboxInput.value = normalized;
  syncHomeOmniboxValueState();
  setHomeAiInputLocked(true);
  setShellPageState("ai");
  setHomeAiPhase(
    HOME_AI_PHASES.PREPARING_PREVIEW,
    t("ai.buildingReadonlyPreview", {}, "正在构建只读 preview；provider call=0。"),
  );
  renderHomeAiPending(HOME_AI_PHASES.PREPARING_PREVIEW);
  void hydrateHomeAiCognitiveBundle(normalized, requestSerial);

  try {
    const params = homeAiPreviewParams({
      question: normalized,
      budget: homeAiState.budget,
      requestId,
      mutationId,
    });
    const preview = await api(`/api/runtime-preview?${params.toString()}`);
    if (requestSerial !== homeAiState.requestSerial) return;
    if (!validateHomeAiPreviewIdentity(preview)) {
      throw new Error(t(
        "error.runtimePreviewIdentity",
        {},
        "Runtime preview 身份校验失败",
      ));
    }
    homeAiState.preview = preview;
    homeAiState.modelRunId = preview.diagnostic_bindings.model_run_id;
    const hasEvidence = homeAiEvidenceItems(preview).length > 0;
    setHomeAiPhase(
      hasEvidence ? HOME_AI_PHASES.PREVIEW_READY : HOME_AI_PHASES.INSUFFICIENT_EVIDENCE,
      hasEvidence
        ? t("ai.previewReadyNotCalled", {}, "Preview 已就绪；尚未调用 provider。")
        : t("ai.insufficientEvidenceDisabled", {}, "证据不足；执行保持禁用。"),
    );
    persistHomeAiSession();
    renderHomeAiPreview(preview);
  } catch (error) {
    if (requestSerial !== homeAiState.requestSerial) return;
    homeAiState.inFlight = false;
    homeAiState.preview = null;
    homeAiState.modelRunId = "";
    clearHomeAiSession();
    renderHomeAiUnavailable(
      t(
        "error.runtimePreviewUnavailable",
        {},
        "无法连接本机 Runtime 预览服务。",
      ),
      {
        technicalDetail: error?.technicalDetail
          || error?.message
          || "RUNTIME_PREVIEW_UNAVAILABLE",
      },
    );
  }
}

async function handleHomeAiSubmit(event) {
  if (event.detail?.mode !== "ai") return;
  if (
    homeAiState.phase !== HOME_AI_PHASES.IDLE
    || homeAiState.inFlight
    || homeAiState.executionAttempted
  ) return;
  const rawQuestion = String(event.detail?.query || "");
  const question = normalizeRuntimeQuestion(rawQuestion);
  if (!question) {
    homeOmniboxInput?.setCustomValidity(t(
      "ai.enterQuestionForPreview",
      {},
      "请输入问题后再构建 AI preview。",
    ));
    homeOmniboxInput?.reportValidity();
    homeOmniboxInput?.focus();
    return;
  }
  homeOmniboxInput?.setCustomValidity("");
  await startHomeAiPreview(question);
}

async function executeHomeAiDocumentOnce() {
  if (
    homeAiState.inFlight
    || homeAiState.executionAttempted
    || homeAiState.phase !== HOME_AI_PHASES.PREVIEW_READY
    || !homeAiState.preview
    || !state.canExecute
    || state.runtimeReview
  ) return;

  homeAiState.inFlight = true;
  homeAiState.executionAttempted = true;
  persistHomeAiSession();
  const requestSerial = homeAiState.requestSerial;
  setHomeAiPhase(
    HOME_AI_PHASES.EXECUTING,
    t(
      "ai.executionLockActive",
      {},
      "执行锁已生效；exactly one request 已提交，自动重试关闭。",
    ),
  );
  renderHomeAiPending(HOME_AI_PHASES.EXECUTING, homeAiState.preview);
  homeAiState.receivingTimer = window.setTimeout(() => {
    if (
      homeAiState.inFlight
      && homeAiState.requestSerial === requestSerial
      && homeAiState.phase === HOME_AI_PHASES.EXECUTING
    ) {
      setHomeAiPhase(
        HOME_AI_PHASES.RECEIVING_RESULT,
        t(
          "ai.waitingRuntimeTerminal",
          {},
          "等待 Runtime ledger 返回终态；不会自动重试。",
        ),
      );
      renderHomeAiPending(HOME_AI_PHASES.RECEIVING_RESULT, homeAiState.preview);
    }
  }, HOME_AI_RECEIVING_DELAY_MS);

  try {
    const envelope = await api("/api/runtime-execute", {
      method: "POST",
      body: JSON.stringify({
        q: homeAiState.preview.question,
        budget: homeAiState.preview.budget_profile,
        request_id: homeAiState.preview.request_id,
        mutation_id: homeAiState.preview.mutation_id,
        expected_bindings: homeAiState.preview.reviewed_execution_binding,
        confirmed: true,
      }),
    });
    if (requestSerial !== homeAiState.requestSerial) return;
    clearHomeAiReceivingTimer();
    homeAiState.inFlight = false;
    if (!["succeeded", "failed"].includes(envelope?.status)) {
      renderHomeAiUnavailable(
        t(
          "error.runtimeNonTerminal",
          {},
          "Runtime 返回非终态。YunOS 不会自动重试；请保留当前 run 供只读审计。",
        ),
        {
          executionUncertain: true,
          envelope,
          technicalDetail: envelope?.status || "RUNTIME_NON_TERMINAL",
        },
      );
      return;
    }
    const result = envelope?.result;
    const savedIdentity = {
      requestId: homeAiState.requestId,
      mutationId: homeAiState.mutationId,
      modelRunId: homeAiState.modelRunId,
    };
    if (!validateHomeAiExecutionEnvelope(envelope, homeAiState.preview, savedIdentity)) {
      throw new Error(t(
        "error.runtimeTerminalIdentity",
        {},
        "Runtime 终态与已确认 preview 身份不一致",
      ));
    }
    homeAiState.envelope = envelope;
    persistHomeAiSession();
    renderHomeAiResult(homeAiState.preview, envelope);
  } catch (error) {
    if (requestSerial !== homeAiState.requestSerial) return;
    clearHomeAiReceivingTimer();
    homeAiState.inFlight = false;
    renderHomeAiUnavailable(
      t(
        "error.executionResultUnconfirmedSummary",
        {},
        "无法安全确认执行结果。系统不会自动重试。",
      ),
      {
        executionUncertain: true,
        technicalDetail: error?.technicalDetail
          || error?.message
          || "RUNTIME_RESULT_UNCONFIRMED",
      },
    );
  }
}

function cancelHomeAiDocument() {
  if (homeAiState.inFlight || homeAiState.executionAttempted) return;
  homeAiState.requestSerial += 1;
  clearHomeAiReceivingTimer();
  clearHomeAiSession();
  renderHomeAiUnavailable(
    t("ai.cancelledBeforeExecution", {}, "用户在执行前取消了本次 AI 任务。"),
    { cancelled: true },
  );
}

function returnToHomeAiIdle() {
  resetHomeAiState();
  setShellPageState("home");
  setHomeOmniboxMode("ai");
  if (homeOmniboxInput) {
    homeOmniboxInput.value = "";
    homeOmniboxInput.setCustomValidity("");
  }
  syncHomeOmniboxValueState();
  homeOmniboxInput?.focus();
}

function openHomeAiSource(item) {
  const anchor = item?.source_anchor || {};
  if (!anchor.conversation_id || !anchor.message_id) return;
  setShellPageState("workspace");
  loadConversation(anchor.conversation_id, {
    focusMessageId: anchor.message_id,
    fromAiDocument: true,
  });
}

function returnToHomeAiDocument() {
  if (!homeAiState.preview || homeAiState.phase === HOME_AI_PHASES.IDLE) {
    returnToHomeAiIdle();
    return;
  }
  setShellPageState("ai");
}

async function restoreHomeAiSession() {
  const saved = readHomeAiSession();
  if (!saved) return;
  clearHomeAiReceivingTimer();
  const requestSerial = ++homeAiState.requestSerial;
  Object.assign(homeAiState, {
    question: saved.question,
    budget: saved.budget,
    requestId: saved.requestId,
    mutationId: saved.mutationId,
    modelRunId: saved.modelRunId,
    preview: null,
    envelope: null,
    executionAttempted: saved.executionAttempted,
    inFlight: false,
    cognitiveBundle: null,
    cognitiveBundleStatus: "loading",
    cognitiveBundleDetail: "",
    runtimeFailure: null,
  });
  setHomeOmniboxMode("ai");
  if (homeOmniboxInput) homeOmniboxInput.value = saved.question;
  syncHomeOmniboxValueState();
  setHomeAiInputLocked(true);
  setShellPageState("ai");
  setHomeAiPhase(
    HOME_AI_PHASES.PREPARING_PREVIEW,
    saved.executionAttempted
      ? t(
        "ai.restoringRuntimeResult",
        {},
        "正在从 Runtime ledger 与 receipt 恢复结果。",
      )
      : t("ai.restoringReadonlyPreview", {}, "正在恢复只读 preview。"),
  );
  renderHomeAiPending(HOME_AI_PHASES.PREPARING_PREVIEW);
  void hydrateHomeAiCognitiveBundle(saved.question, requestSerial);

  try {
    let storedResult = null;
    if (saved.executionAttempted) {
      storedResult = await api(
        `/api/runtime-run?model_run_id=${encodeURIComponent(saved.modelRunId)}`,
      );
    }
    const params = homeAiPreviewParams({
      question: saved.question,
      budget: saved.budget,
      requestId: saved.requestId,
      mutationId: saved.mutationId,
    });
    const preview = await api(`/api/runtime-preview?${params.toString()}`);
    if (requestSerial !== homeAiState.requestSerial) return;
    if (!validateHomeAiPreviewIdentity(preview, saved.modelRunId)) {
      throw new Error(t(
        "error.restoredPreviewIdentity",
        {},
        "重建 preview 与原 model run 身份不一致",
      ));
    }
    homeAiState.preview = preview;
    if (saved.executionAttempted) {
      if (!["succeeded", "failed"].includes(storedResult.status)) {
        throw new Error(t(
          "error.runtimeRunNotRestorable",
          {},
          "Runtime run 尚未进入可安全恢复的终态",
        ));
      }
      if (
        !validateHomeAiTerminalResult(storedResult)
        || !validateHomeAiRecoveredResult(preview, storedResult, saved)
      ) {
        throw new Error(t(
          "error.runtimeArtifactDrift",
          {},
          "Runtime artifact、receipt、来源或 Reviewed Binding 已发生漂移",
        ));
      }
      homeAiState.envelope = storedResult;
      renderHomeAiResult(preview, storedResult, { recovered: true });
      return;
    }
    const hasEvidence = homeAiEvidenceItems(preview).length > 0;
    setHomeAiPhase(
      hasEvidence ? HOME_AI_PHASES.PREVIEW_READY : HOME_AI_PHASES.INSUFFICIENT_EVIDENCE,
      hasEvidence
        ? t(
          "ai.previewRestoredNoCall",
          {},
          "Preview 已从 session 标识重建；provider call=0。",
        )
        : t("ai.insufficientEvidence", {}, "证据不足。"),
    );
    renderHomeAiPreview(preview);
  } catch (error) {
    if (requestSerial !== homeAiState.requestSerial) return;
    renderHomeAiUnavailable(
      t(
        "error.safeRestoreSummary",
        {},
        "无法安全恢复之前的结果。旧结果不会与新来源拼接。",
      ),
      {
        technicalDetail: error?.technicalDetail
          || error?.message
          || "RUNTIME_RESTORE_FAILED",
      },
    );
  }
}

function returnToHomeMemorySearch() {
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  setShellPageState("home");
  if (homeMemorySearchState.items.length) {
    renderHomeMemorySearchResults(homeMemorySearchState.items, homeMemorySearchState.query);
  }
  homeOmniboxInput?.focus();
}

setHomeOmniboxMode(readHomeOmniboxMode());
syncHomeOmniboxValueState();

homeOmniboxModeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    setHomeOmniboxMode(button.dataset.omniboxMode, { persist: true });
  });
});

homeOmniboxInput?.addEventListener("input", () => {
  homeOmniboxInput.setCustomValidity("");
  syncHomeOmniboxValueState();
  if (homeMemorySearchState.open) closeHomeMemorySearchOverlay({ invalidateRequest: true });
});

homeOmniboxForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  const mode = HOME_OMNIBOX_MODES.has(homeOmniboxForm.dataset.mode)
    ? homeOmniboxForm.dataset.mode
    : HOME_OMNIBOX_DEFAULT_MODE;
  homeOmniboxForm.dispatchEvent(new CustomEvent("yunos:home-omnibox-submit", {
    bubbles: true,
    cancelable: true,
    detail: { mode, query: homeOmniboxInput?.value || "" },
  }));
});

homeOmniboxForm?.addEventListener("yunos:home-omnibox-submit", handleHomeMemorySearchSubmit);
homeOmniboxForm?.addEventListener("yunos:home-omnibox-submit", handleHomeAiSubmit);

homeOmniboxForm?.addEventListener("keydown", (event) => {
  if (homeMemorySearchState.open && homeMemorySearchState.items.length) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      setHomeMemorySearchActive(homeMemorySearchState.activeIndex + direction, { scroll: true });
      return;
    }
    if (event.key === "Enter" && homeMemorySearchState.activeIndex >= 0) {
      event.preventDefault();
      openHomeMemorySearchResult();
      return;
    }
  }
  if (event.key !== "Escape") return;
  if (homeMemorySearchState.open) {
    event.preventDefault();
    closeHomeMemorySearchOverlay({ invalidateRequest: true });
    return;
  }
  if (desktopShell?.dataset.sidebarOpen === "true") return;
  event.preventDefault();
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
});

function clearShellSidebarTimer(timerName) {
  if (!shellSidebarState[timerName]) return;
  window.clearTimeout(shellSidebarState[timerName]);
  shellSidebarState[timerName] = 0;
}

function syncShellSidebar({ returnFocus = false } = {}) {
  if (!desktopShell || !shellSidebar || !shellSidebarToggle) return;
  const homeHoverOpen = desktopShell.dataset.pageState === "home" && shellSidebarState.hover;
  const isOpen = shellSidebarState.pinned || homeHoverOpen;
  const trigger = shellSidebarState.pinned ? "pinned" : homeHoverOpen ? "hover" : "none";
  desktopShell.dataset.sidebarOpen = String(isOpen);
  desktopShell.dataset.sidebarPinned = String(shellSidebarState.pinned);
  desktopShell.dataset.sidebarTrigger = trigger;
  shellSidebar.setAttribute("aria-hidden", String(!isOpen));
  shellSidebar.toggleAttribute("inert", !isOpen);
  shellSidebarToggle.setAttribute("aria-expanded", String(isOpen));
  shellSidebarToggle.setAttribute(
    "aria-label",
    isOpen
      ? t("aria.collapseSidebar", {}, "收起 YunOS 侧栏")
      : t("aria.expandSidebar", {}, "展开 YunOS 侧栏"),
  );
  if (!isOpen && returnFocus) shellSidebarToggle.focus();
  else if (!isOpen && shellSidebar.contains(document.activeElement)) shellSidebarToggle.focus();
}

function setShellSidebarOpen(open, { trigger = "pinned", returnFocus = false } = {}) {
  const isOpen = Boolean(open);
  if (trigger === "hover") {
    shellSidebarState.hover = isOpen;
  } else if (trigger === "all") {
    shellSidebarState.hover = false;
    shellSidebarState.pinned = false;
  } else {
    shellSidebarState.pinned = isOpen;
    if (isOpen) shellSidebarState.hover = false;
  }
  syncShellSidebar({ returnFocus });
}

function scheduleShellSidebarHoverOpen() {
  if (desktopShell?.dataset.pageState !== "home" || shellSidebarState.pinned) return;
  clearShellSidebarTimer("closeTimer");
  clearShellSidebarTimer("openTimer");
  shellSidebarState.openTimer = window.setTimeout(() => {
    shellSidebarState.openTimer = 0;
    if (shellSidebarState.pointerInside && desktopShell?.dataset.pageState === "home") {
      setShellSidebarOpen(true, { trigger: "hover" });
    }
  }, SIDEBAR_HOVER_OPEN_DELAY_MS);
}

function scheduleShellSidebarHoverClose() {
  clearShellSidebarTimer("openTimer");
  if (shellSidebarState.pinned) return;
  clearShellSidebarTimer("closeTimer");
  shellSidebarState.closeTimer = window.setTimeout(() => {
    shellSidebarState.closeTimer = 0;
    if (shellSidebarState.pointerInside || shellSidebar?.contains(document.activeElement)) return;
    setShellSidebarOpen(false, { trigger: "hover" });
  }, SIDEBAR_HOVER_CLOSE_DELAY_MS);
}

function setShellPageState(nextState) {
  legacyViewRequestSerial += 1;
  if (!desktopShell) return;
  const pageState = nextState === "home" || nextState === "ai" ? nextState : "workspace";
  const activeRegion = pageState === "workspace" ? "workspace" : "home";
  desktopShell.dataset.pageState = pageState;
  desktopShell.dataset.sidebarMode = pageState === "home" ? "overlay" : "push";
  if (pageState !== "home") {
    clearShellSidebarTimer("openTimer");
    clearShellSidebarTimer("closeTimer");
    shellSidebarState.hover = false;
    shellSidebarState.pointerInside = false;
  }
  shellRegions.forEach((region) => {
    const isActive = region.dataset.shellRegion === activeRegion;
    region.setAttribute("aria-hidden", String(!isActive));
    region.toggleAttribute("inert", !isActive);
  });
  syncShellSidebar();
  renderSidebarContext();
}

setShellPageState(desktopShell?.dataset.pageState || "workspace");

const state = {
  currentView: "recent",
  dbMode: "unknown",
  readonly: false,
  serviceReadonly: false,
  serviceMode: "unknown",
  runtimeReview: false,
  runtimeAdmin: false,
  runtimeMock: false,
  executionAuthorityPresent: false,
  canExecute: false,
  boundHost: "",
  boundPort: 0,
  boundEndpoint: "",
  processId: null,
  serverInstanceId: "",
  dbPathLabel: "",
  lastSearchQuery: "",
  lastSearchSourcePlatform: "",
  lastSearchMemorySpace: "whale",
  lastSearchItems: [],
  lastContext: null,
  lastProject: null,
  lastProjectSourcePlatform: "",
  lastTimeline: { groupBy: "month", project: "", q: "", sourcePlatform: "", items: [] },
  lastSummaries: { memorySpace: "whale", summaryType: "", items: [], health: null, healthError: "" },
  lastContextReview: {
    query: "",
    startTime: "",
    endTime: "",
    budget: "Medium",
    totalCharacters: 6000,
    estimatedTokens: 1500,
    maxEvidence: 12,
    maxCandidates: 6,
    maxHistoryExpansions: 0,
    excludedItemIds: [],
    data: null,
  },
  lastApprovedProfileControl: {
    query: "",
    budget: "Medium",
    data: null,
  },
  lastRuntimeExecution: {
    question: "",
    budget: "Medium",
    requestId: "",
    mutationId: "",
    preview: null,
    result: null,
  },
  lastPromptInspector: {
    input: "",
    summaryLimit: 5,
    rawMemoryTopK: 5,
    includeGeneral: false,
    previewChars: 300,
    showFullPrompt: false,
    data: null,
  },
  lastContextOptimizer: {
    input: "",
    testGeneral: false,
    limit: 5,
    maxTokens: "",
    data: null,
  },
  lastConversationSource: {},
  selectedReference: null,
};

const ENABLE_AUTO_ENTER = true;
const AUTO_ENTER_DELAY = 0;
const EXIT_TRANSITION_MS = 0;
const ENTER_ON_ANY_CLICK = false;

window.addEventListener("load", () => {
  const splash = document.getElementById("splash") || document.getElementById("splash-screen");
  if (!splash) return;

  let closed = false;

  const closeSplash = () => {
    if (closed) return;
    closed = true;
    splash.classList.add("is-hidden");
    setTimeout(() => splash.remove(), EXIT_TRANSITION_MS + 80);
  };

  if (ENTER_ON_ANY_CLICK) {
    splash.addEventListener("click", closeSplash);
  }

  if (ENABLE_AUTO_ENTER) {
    setTimeout(closeSplash, AUTO_ENTER_DELAY);
  }
});

const statuses = ["keep", "trash", "project", "study", "writing", "life", "tech", "review"];
const statusLabelKeys = {
  keep: ["status.keep", "保留"],
  trash: ["status.trash", "废弃"],
  project: ["common.project", "项目"],
  study: ["status.study", "学习"],
  writing: ["status.writing", "写作"],
  life: ["status.life", "生活"],
  tech: ["status.tech", "技术"],
  review: ["status.review", "待整理"],
};
const roleLabelKeys = {
  user: ["common.user", "用户"],
  assistant: ["common.assistant", "助手"],
  system: ["common.system", "系统"],
  tool: ["common.tool", "工具"],
};
const sourceLabelKeys = {
  title: ["common.title", "标题"],
  message: ["common.messageBody", "消息正文"],
};
const projectStatusLabelKeys = {
  active: ["status.active", "进行中"],
  paused: ["status.paused", "已暂停"],
  archived: ["status.archived", "已归档"],
};
const sourcePlatformOptions = [
  ["", "source.all", "全部来源"],
  ["chatgpt", "shell.extra.sourcePlatform.chatgpt", "ChatGPT"],
  ["deepseek", "shell.extra.sourcePlatform.deepseek", "DeepSeek"],
  ["claude", "shell.extra.sourcePlatform.claude", "Claude"],
  ["gemini", "shell.extra.sourcePlatform.gemini", "Gemini"],
  ["manual", "source.manual", "手动记录"],
  ["markdown", "shell.extra.sourcePlatform.markdown", "Markdown"],
  ["txt", "shell.extra.sourcePlatform.txt", "TXT"],
];
const sourcePlatformLabels = Object.fromEntries(
  sourcePlatformOptions.filter(([value]) => value).map(([value, key, fallback]) => [
    value,
    key ? t(key, {}, fallback) : fallback,
  ]),
);
const memorySpaceOptions = [
  ["whale", "memory.whale", "鲸鱼记忆"],
  ["general", "memory.general", "通用记忆"],
];
const memorySpaceLabels = Object.fromEntries(
  memorySpaceOptions.map(([value, key, fallback]) => [value, t(key, {}, fallback)]),
);
const summaryTypeOptions = [
  ["", "summary.all", "全部摘要"],
  ["conversation_summary", "summary.conversation", "单会话摘要"],
  ["memory_space_overview", "summary.memorySpaceOverview", "记忆空间总览"],
];
const summaryTypeLabels = Object.fromEntries(
  summaryTypeOptions.filter(([value]) => value).map(([value, key, fallback]) => [
    value,
    t(key, {}, fallback),
  ]),
);

function text(value) {
  return value == null ? "" : String(value);
}

function labelStatus(status) {
  const entry = statusLabelKeys[status];
  return entry
    ? t(entry[0], {}, entry[1])
    : (status ? status : t("status.unmarked", {}, "未标记"));
}

function statusFromInput(value) {
  const raw = text(value).trim();
  if (statuses.includes(raw)) return raw;
  const found = Object.entries(statusLabelKeys).find(([key]) => labelStatus(key) === raw);
  return found ? found[0] : "";
}

function labelRole(role, roleLabel = "") {
  const entry = roleLabelKeys[role];
  return roleLabel
    || (entry ? t(entry[0], {}, entry[1]) : "")
    || role
    || t("status.unknown", {}, "未知");
}

function labelSource(source) {
  const entry = sourceLabelKeys[source];
  return (entry ? t(entry[0], {}, entry[1]) : "")
    || source
    || t("status.unknown", {}, "未知");
}

function labelProjectStatus(status) {
  const entry = projectStatusLabelKeys[status];
  return (entry ? t(entry[0], {}, entry[1]) : "")
    || status
    || t("status.active", {}, "进行中");
}

function labelSourcePlatform(platform, label = "") {
  return label
    || sourcePlatformLabels[platform]
    || t("source.unknown", {}, "未知来源");
}

function sourcePlatformSelect(id, value = "") {
  const select = el("select");
  select.id = id;
  sourcePlatformOptions.forEach(([optionValue, key, fallback]) => {
    const option = el("option", "", key ? t(key, {}, fallback) : fallback);
    option.value = optionValue;
    select.appendChild(option);
  });
  select.value = value || "";
  return select;
}

function sourceParam(value) {
  return value ? `&source_platform=${encodeURIComponent(value)}` : "";
}

function memorySpaceSelect(id, value = "whale") {
  const select = el("select");
  select.id = id;
  memorySpaceOptions.forEach(([optionValue, key, fallback]) => {
    const option = el("option", "", t(key, {}, fallback));
    option.value = optionValue;
    select.appendChild(option);
  });
  select.value = value || "whale";
  return select;
}

function memoryParam(value) {
  return `&memory_space=${encodeURIComponent(value || "whale")}`;
}

function summaryTypeSelect(id, value = "") {
  const select = el("select");
  select.id = id;
  summaryTypeOptions.forEach(([optionValue, key, fallback]) => {
    const option = el("option", "", t(key, {}, fallback));
    option.value = optionValue;
    select.appendChild(option);
  });
  select.value = value || "";
  return select;
}

function labelSummaryType(value) {
  return summaryTypeLabels[value] || value || t("summary.summary", {}, "摘要");
}

function sourceCountsText(sourceCounts = {}) {
  const entries = Object.entries(sourceCounts || {}).filter(([, count]) => Number(count) > 0);
  if (!entries.length) return "";
  if (entries.length === 1) {
    return t(
      "source.single",
      { source: labelSourcePlatform(entries[0][0]) },
      `来源：${labelSourcePlatform(entries[0][0])}`,
    );
  }
  const values = entries.map(([platform, count]) => (
    `${labelSourcePlatform(platform)} ${formatUiNumber(count)}`
  )).join("，");
  return t("source.multiple", { sources: values }, `来源：${values}`);
}

function labelDbMode(mode = state.dbMode) {
  if (mode === "real" && state.readonly) {
    return t("database.realReadonly", {}, "真实记忆库（只读）");
  }
  return mode === "real"
    ? t("database.real", {}, "真实记忆库")
    : t("database.test", {}, "测试库");
}

function buildReference(item) {
  const conversationId = item.conversation_id || "";
  const messageId = item.message_id || "";
  if (item.reference_uri) return item.reference_uri;
  if (!conversationId) return "";
  return messageId ? `ayunos://conversation/${conversationId}?message=${messageId}` : `ayunos://conversation/${conversationId}`;
}

function setTitle(value) {
  title.textContent = value;
}

function setActive(view) {
  legacyViewRequestSerial += 1;
  state.currentView = view;
  shellSidebarNav?.querySelectorAll("[data-view]").forEach((item) => {
    const isActive = item.dataset.view === view;
    item.classList.toggle("active", isActive);
    if (isActive) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
}

function beginLegacyViewRequest(view = state.currentView) {
  return { view, serial: ++legacyViewRequestSerial };
}

function isLegacyViewRequestCurrent(request) {
  return request.serial === legacyViewRequestSerial
    && request.view === state.currentView
    && desktopShell?.dataset.pageState === "workspace";
}

function el(tag, className = "", value = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== "") node.textContent = value;
  return node;
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      ...options,
    });
    window.dispatchEvent(new CustomEvent("yunos:host-connectivity", {
      detail: { available: true },
    }));
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    window.dispatchEvent(new CustomEvent("yunos:host-connectivity", {
      detail: { available: false },
    }));
    const networkError = new Error(t(
      "error.hostUnavailable",
      {},
      "无法连接 YunOS 主机",
    ), { cause: error });
    networkError.isNetworkError = true;
    networkError.technicalDetail = error?.name || "NETWORK_ERROR";
    throw networkError;
  }
  let data = {};
  try {
    data = await response.json();
  } catch (cause) {
    const responseError = new Error(
      t("error.invalidJson", {}, "服务器响应不是有效 JSON"), { cause },
    );
    responseError.status = response.status;
    responseError.technicalDetail = "INVALID_JSON";
    throw responseError;
  }
  if (!response.ok) {
    const responseError = new Error(data.error || t("error.requestFailed", {}, "请求失败"));
    responseError.status = response.status;
    responseError.technicalDetail = data.error || `HTTP_${response.status}`;
    throw responseError;
  }
  return data;
}

async function messageApi(path, { signal = null } = {}) {
  const controller = new AbortController();
  let timedOut = false;
  const forwardAbort = () => controller.abort(signal?.reason);
  signal?.addEventListener("abort", forwardAbort, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, MOBILE_MESSAGE_REQUEST_TIMEOUT_MS);
  try {
    return await api(path, { signal: controller.signal });
  } catch (error) {
    if (timedOut) {
      const timeoutError = new Error(t("error.requestTimeout", {}, "请求超时"));
      timeoutError.isTimeout = true;
      throw timeoutError;
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener("abort", forwardAbort);
  }
}

function allowsPrivateCacheFallback(error) {
  return Boolean(
    error?.isNetworkError
    || error?.isTimeout
    || [502, 503, 504].includes(Number(error?.status)),
  );
}

async function loadStatus() {
  const data = await api("/api/status");
  state.dbMode = data.db_mode || "unknown";
  state.dbPathLabel = data.db_path_label || "";
  state.runtimeReview = data.runtime_review === true;
  state.runtimeAdmin = Boolean(data.runtime_admin);
  state.runtimeMock = Boolean(data.runtime_mock);
  state.executionAuthorityPresent = data.execution_authority_present === true;
  state.canExecute = data.can_execute === true;
  state.serviceReadonly = Boolean(data.readonly);
  state.readonly = Boolean(data.readonly_source_tables ?? data.readonly);
  state.serviceMode = data.service_mode || (state.runtimeAdmin
    ? (state.runtimeMock ? "runtime-admin-mock" : "runtime-admin")
    : state.runtimeReview
      ? "runtime-review"
      : (state.dbMode === "real" && state.readonly ? "real-readonly" : "web"));
  state.boundHost = data.bound_host || window.location.hostname;
  state.boundPort = Number(data.bound_port || window.location.port || 0);
  state.boundEndpoint = data.bound_endpoint || window.location.origin;
  state.processId = data.process_id ?? null;
  state.serverInstanceId = data.server_instance_id || "";
  dbMode.textContent = t(
    "database.mode",
    { mode: labelDbMode() },
    `数据库模式：${labelDbMode()}`,
  );
  dbMode.title = state.dbPathLabel;
  dbWarning.textContent = state.runtimeAdmin
    ? t(
      "runtime.adminIsolationWarning",
      {},
      "Runtime-admin 已隔离到独立监听端点；真实记忆源保持只读，只有显式确认的执行会追加 Runtime ledger。",
    )
    : state.readonly
      ? t(
        "database.readonlyNotice",
        {},
        "当前正在浏览真实本地记忆库。只读模式已启用，系统不会写入真实数据。",
      )
      : t(
        "database.writeWarning",
        {},
        "警告：你正在使用真实本地记忆库，修改会写入真实数据。",
      );
  dbWarning.classList.toggle("hidden", state.dbMode !== "real");
}

function readonlyNotice() {
  if (!state.readonly) return null;
  const notice = el("section", "readonly-notice");
  notice.textContent = t(
    "database.readonlyNotice",
    {},
    "当前正在浏览真实本地记忆库。只读模式已启用，系统不会写入真实数据。",
  );
  return notice;
}

function appendReadonlyNotice(target = content) {
  const notice = readonlyNotice();
  if (notice) target.appendChild(notice);
}

function renderEmpty(message = t("common.noResults", {}, "没有找到结果。"), target = content) {
  target.innerHTML = "";
  target.appendChild(el("div", "empty-card", message));
}

function technicalErrorDisclosure(error) {
  const detail = String(
    error?.technicalDetail
    || error?.message
    || error
    || "",
  ).trim();
  if (!detail) return null;
  const disclosure = el("details", "yunos-technical-error-details");
  disclosure.append(
    el("summary", "", t("common.technicalDetails", {}, "技术详情")),
    el("code", "", detail),
  );
  return disclosure;
}

function renderTechnicalError(
  error,
  target = content,
  summaryKey = "error.loadFailedRetry",
  fallback = "内容加载失败，请重试。",
) {
  target.innerHTML = "";
  const card = el("div", "empty-card");
  card.appendChild(el("p", "", t(summaryKey, {}, fallback)));
  const disclosure = technicalErrorDisclosure(error);
  if (disclosure) card.appendChild(disclosure);
  target.appendChild(card);
}

function setMessage(element, message, kind = "meta") {
  element._yunosTechnicalErrorDisclosure?.remove();
  element._yunosTechnicalErrorDisclosure = null;
  element.className = kind === "error" ? "form-message error" : "form-message";
  element.textContent = message;
}

function setTechnicalMessage(
  element,
  error,
  summaryKey = "error.saveFailedRetry",
  fallback = "更改未能保存，请重试。",
) {
  setMessage(element, t(summaryKey, {}, fallback), "error");
  const disclosure = technicalErrorDisclosure(error);
  if (!disclosure) return;
  element.insertAdjacentElement("afterend", disclosure);
  element._yunosTechnicalErrorDisclosure = disclosure;
}

function appendHighlighted(parent, value, keyword) {
  const haystack = text(value);
  const needle = text(keyword);
  if (!needle) {
    parent.textContent = haystack;
    return;
  }
  const lowerHaystack = haystack.toLowerCase();
  const lowerNeedle = needle.toLowerCase();
  let cursor = 0;
  let index = lowerHaystack.indexOf(lowerNeedle);
  while (index !== -1) {
    if (index > cursor) parent.appendChild(document.createTextNode(haystack.slice(cursor, index)));
    const mark = el("mark");
    mark.textContent = haystack.slice(index, index + needle.length);
    parent.appendChild(mark);
    cursor = index + needle.length;
    index = lowerHaystack.indexOf(lowerNeedle, cursor);
  }
  if (cursor < haystack.length) parent.appendChild(document.createTextNode(haystack.slice(cursor)));
}

function appendHighlightedRanges(parent, value, ranges = []) {
  const source = String(value || "");
  const normalizedRanges = (Array.isArray(ranges) ? ranges : [])
    .map((range) => ({
      start: Math.max(0, Number(range?.start) || 0),
      end: Math.min(source.length, Number(range?.end) || 0),
    }))
    .filter((range) => range.end > range.start)
    .sort((left, right) => left.start - right.start);
  if (!normalizedRanges.length) {
    parent.textContent = source;
    return;
  }
  let cursor = 0;
  normalizedRanges.forEach((range) => {
    if (range.start > cursor) {
      parent.appendChild(document.createTextNode(source.slice(cursor, range.start)));
    }
    const start = Math.max(cursor, range.start);
    if (range.end > start) {
      const mark = el("mark");
      mark.textContent = source.slice(start, range.end);
      parent.appendChild(mark);
    }
    cursor = Math.max(cursor, range.end);
  });
  if (cursor < source.length) {
    parent.appendChild(document.createTextNode(source.slice(cursor)));
  }
}

function cleanMobileSearchSnippet(value) {
  const withoutMarkdownTicks = String(value || "")
    .replace(/\u0060{3}(?:[A-Za-z0-9_.+-]+)?/gu, " ")
    .replace(/\u0060+/gu, "");
  const withoutMarkdownStructure = withoutMarkdownTicks
    .replace(/!\[([^\]]*)\]\([^)]*\)/gu, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/gu, "$1")
    .replace(/(^|\s)(?:#{1,6}|>{1,3})(?=\s)/gu, " ")
    .replace(/(?:[-*_~=]){3,}/gu, " ")
    .replace(/\|+/gu, " ")
    .replace(/(^|\s)[*+-](?=\s)/gu, " ")
    .replace(/(?:\*{2,}|_{2,}|~{2,})/gu, "");
  return withoutMarkdownStructure.replace(/\s+/gu, " ").trim();
}

function mobileVisibleCharacterCount(value) {
  return Array.from(String(value || "").trim()).filter((character) => !/\s/u.test(character)).length;
}

function mobileFirstPageKey(query = mobileSearchState.query, sort = mobileSearchState.sort) {
  return `${String(query || "").trim()}\u0000${sort}`;
}

function isMobileHostAvailable() {
  return navigator.onLine && document.body.dataset.yunosHostAvailable !== "false";
}

function mobileStableRef(hit = {}) {
  const conversationId = String(hit.conversation_id || "").trim();
  const messageId = String(hit.message_id || "").trim();
  return conversationId && messageId ? { conversationId, messageId } : null;
}

function currentMobileResultRefs() {
  const seen = new Set();
  const refs = [];
  mobileSearchState.groups.forEach((group) => {
    [group.best_hit, ...(group.other_hits || [])].forEach((hit) => {
      const ref = mobileStableRef(hit);
      if (!ref) return;
      const token = `${ref.conversationId}\u0000${ref.messageId}`;
      if (seen.has(token)) return;
      seen.add(token);
      refs.push(ref);
    });
  });
  return refs;
}

function renderMobileOfflineSearchBanner() {
  if (!mobileOfflineSearchBanner) return;
  const cachedOnly = mobileSearchState.sourceMode === "cached-only";
  mobileOfflineSearchBanner.hidden = !cachedOnly;
  if (!cachedOnly) return;
  const coverage = mobileSearchState.cacheCoverage || {};
  if (mobileOfflineSearchCoverage) {
    const conversationCount = formatUiNumber(Number(coverage.conversationCount) || 0);
    const messageCount = formatUiNumber(Number(coverage.messageCount) || 0);
    mobileOfflineSearchCoverage.textContent = t(
      "offline.cacheCoverage",
      { conversations: conversationCount, messages: messageCount },
      `${conversationCount} 个缓存会话 · ${messageCount} 条可搜索消息`,
    );
  }
  if (mobileOfflineSearchUpdated) {
    mobileOfflineSearchUpdated.textContent = Number(coverage.lastUpdatedAt)
      ? t(
        "offline.recentlyCachedAt",
        { time: formatMobileCacheTime(coverage.lastUpdatedAt, { prefix: false }) },
        `最近缓存于 ${formatMobileCacheTime(coverage.lastUpdatedAt, { prefix: false })}`,
      )
      : t("offline.cacheTimeUnknown", {}, "最近缓存时间未知");
  }
  if (mobileSearchAllHost) {
    mobileSearchAllHost.hidden = !isMobileHostAvailable();
  }
}

function firstVisibleMobileResultRef() {
  if (!mobileResultsPage || !mobileSearchResults) return null;
  const bounds = mobileResultsPage.getBoundingClientRect();
  const unit = [...mobileSearchResults.querySelectorAll(".yunos-mobile-result-unit")]
    .find((candidate) => candidate.getBoundingClientRect().bottom > bounds.top + 1);
  if (!unit) return null;
  try {
    const [conversationId, messageId] = JSON.parse(unit.dataset.mobileHitKey || "[]");
    if (!conversationId || !messageId) return null;
    return {
      conversationId,
      messageId,
      offset: unit.getBoundingClientRect().top - bounds.top,
    };
  } catch {
    return null;
  }
}

function mobileReaderScrollAnchor() {
  const node = firstVisibleMobileReaderMessage();
  if (!node || !mobileReaderScroll) return null;
  return {
    messageId: node.dataset.messageId || "",
    offset: node.getBoundingClientRect().top
      - mobileReaderScroll.getBoundingClientRect().top,
  };
}

function buildMobileRestoreState() {
  const view = mobileShell?.dataset.mobileView || "home";
  const resultAnchor = firstVisibleMobileResultRef();
  const readerAnchor = mobileReaderScrollAnchor();
  const expandedPreview = (() => {
    try {
      const [conversationId, messageId] = JSON.parse(
        mobileSearchState.expandedPreviewKey || "[]",
      );
      return conversationId && messageId ? { conversationId, messageId } : null;
    } catch {
      return null;
    }
  })();
  return {
    savedAt: mobileStatePersistence.restoredSavedAt,
    lastInteractionAt: mobileStatePersistence.lastInteractionAt,
    route: view === "reader" ? "reader" : (view === "results" ? "results" : "home"),
    sourceMode: mobileSearchState.sourceMode,
    query: mobileSearchState.submittedQuery || mobileSearchState.query,
    sortMode: mobileSearchState.sort,
    loadedResultRefs: currentMobileResultRefs(),
    resultAnchorRef: resultAnchor
      ? {
        conversationId: resultAnchor.conversationId,
        messageId: resultAnchor.messageId,
      }
      : null,
    resultAnchorOffset: resultAnchor?.offset || 0,
    resultScrollTop: mobileResultsPage?.scrollTop || mobileSearchState.resultsScrollTop || 0,
    expandedConversationId: mobileSearchState.expandedConversationId,
    expandedMatchIds: currentMobileResultRefs().filter(
      (ref) => ref.conversationId === mobileSearchState.expandedConversationId,
    ),
    expandedPreviewRef: expandedPreview,
    currentConversationId: mobileConversationState.conversationId,
    currentAnchorMessageId: mobileConversationState.focusMessageId,
    readerScrollAnchorMessageId: readerAnchor?.messageId || "",
    readerScrollOffset: readerAnchor?.offset || 0,
    topBarHidden: mobileConversationReader?.dataset.topbarVisible === "false",
    orientation: window.innerWidth > window.innerHeight ? "landscape" : "portrait",
    pendingRefresh: Boolean(
      mobileSearchState.restoring
      || mobileConversationState.revalidating,
    ),
  };
}

async function saveMobileRestoreState({ interaction = false } = {}) {
  if (!isMobileSearchSurfaceActive()) return { saved: false, reason: "desktop" };
  const cache = await mobilePrivateCacheReady;
  if (!cache) return { saved: false, reason: "cache_unavailable" };
  if (interaction && Date.now() < mobileStatePersistence.suppressInteractionUntil) {
    interaction = false;
  }
  if (interaction) {
    mobileStatePersistence.hasUserInteraction = true;
    mobileStatePersistence.lastInteractionAt = Date.now();
  }
  const result = await cache.saveResumeState(buildMobileRestoreState(), {
    advanceSavedAt: interaction || (
      mobileStatePersistence.hasUserInteraction
      && !mobileStatePersistence.restoredSavedAt
    ),
  });
  if (result?.saved) {
    mobileStatePersistence.restoredSavedAt = Number(result.state?.savedAt) || 0;
  }
  return result;
}

function scheduleMobileRestoreStateSave({
  interaction = true,
  delay = MOBILE_STATE_SAVE_DEBOUNCE_MS,
} = {}) {
  if (interaction && Date.now() < mobileStatePersistence.suppressInteractionUntil) {
    interaction = false;
  }
  if (interaction) {
    mobileStatePersistence.hasUserInteraction = true;
    mobileStatePersistence.lastInteractionAt = Date.now();
  }
  window.clearTimeout(mobileStatePersistence.saveTimer);
  mobileStatePersistence.saveTimer = window.setTimeout(() => {
    mobileStatePersistence.saveTimer = 0;
    saveMobileRestoreState({ interaction }).catch((error) => {
      console.warn("YunOS mobile resume state write failed.", {
        code: error?.code || error?.name || "resume_write_failed",
      });
    });
  }, Math.max(0, delay));
}

function isMobileSearchSurfaceActive() {
  return window.matchMedia?.(
    "(max-width: 767px), (max-width: 950px) and (max-height: 560px)",
  ).matches === true;
}

function clearMobileSearchDebounce() {
  if (!mobileSearchState.debounceTimer) return;
  window.clearTimeout(mobileSearchState.debounceTimer);
  mobileSearchState.debounceTimer = 0;
}

function abortMobileSearchRequest({ invalidate = false } = {}) {
  if (mobileSearchState.abortController) mobileSearchState.abortController.abort();
  mobileSearchState.abortController = null;
  mobileSearchState.activeRequestKey = "";
  mobileSearchState.loading = false;
  if (invalidate) mobileSearchState.requestSerial += 1;
}

function setMobileSearchHint(message = "", kind = "status") {
  if (!mobileSearchHint) return;
  mobileSearchHint.textContent = message;
  mobileSearchHint.hidden = !message;
  mobileSearchHint.classList.toggle("is-error", kind === "error");
}

function setMobileSearchStatus(message = "", kind = "status") {
  if (!mobileSearchStatus) return;
  mobileSearchStatus.textContent = message;
  mobileSearchStatus.classList.toggle("is-error", kind === "error");
  mobileSearchStatus.classList.toggle("is-loading", kind === "loading");
  mobileSearchStatus.setAttribute("role", kind === "error" ? "alert" : "status");
}

function mobileCognitiveCard(titleText, value) {
  const card = el("section", "yunos-mobile-cognitive-card");
  card.append(
    el("h2", "", titleText),
    el("div", "yunos-mobile-cognitive-card-body", String(value || "—")),
  );
  return card;
}

function renderMobileCognitiveUnavailable(message = "") {
  if (!mobileCognitiveContent) return;
  mobileCognitiveContent.replaceChildren();
  const stateCard = el("section", "yunos-mobile-cognitive-state is-unavailable");
  stateCard.append(
    el(
      "h2",
      "",
      t("cognitive.workspaceTitle", {}, "Cognitive Task Workspace"),
    ),
    el(
      "p",
      "",
      message || t(
        "cognitive.mobileUnavailable",
        {},
        "No cognitive task can be displayed safely. Cached data will not be presented as live state.",
      ),
    ),
    el(
      "p",
      "yunos-mobile-cognitive-boundary",
      t(
        "cognitive.mobileReadonlyNotice",
        {},
        "Mobile is read-only. Tasks, authority, high-risk acceptance, and write tools are unavailable here.",
      ),
    ),
  );
  mobileCognitiveContent.appendChild(stateCard);
}

function renderMobileCognitiveWorkspace(bundle) {
  if (!mobileCognitiveContent) return;
  const model = cognitiveWorkspaceModel(
    null,
    null,
    HOME_AI_PHASES.PREPARING_PREVIEW,
    bundle,
  );
  const planNodes = Array.isArray(model.plan.nodes) ? model.plan.nodes : [];
  const executions = cognitiveCollectionItems(model.parts.root.executions);
  const verifications = model.parts.verifications;
  mobileCognitiveContent.replaceChildren();
  const article = el("article", "yunos-mobile-cognitive-document");
  article.append(
    el("p", "yunos-mobile-cognitive-live", t(
      "cognitive.mobileCurrentAuthoritative",
      {},
      "This content comes from a current no-store authoritative read and does not use offline cache.",
    )),
    el("h2", "yunos-mobile-cognitive-task-title", model.title),
  );
  const meta = el("dl", "yunos-mobile-cognitive-meta");
  [
    [t("cognitive.project", {}, "Project"), model.project],
    [t("cognitive.status", {}, "Status"), model.status],
    [t("cognitive.provider", {}, "Provider"), model.provider],
    [t("cognitive.agencyLevel", {}, "Agency Level"), cognitiveAgencyLabel(model.agencyLevel)],
    [t("cognitive.cognitiveStance", {}, "Cognitive Stance"), cognitiveStanceLabel(model.cognitiveStance)],
    [t("cognitive.risk", {}, "Risk"), cognitiveRiskLabel(model.risk)],
  ].forEach(([label, value]) => {
    const item = el("div", "");
    item.append(el("dt", "", label), el("dd", "", String(value || "—")));
    meta.appendChild(item);
  });
  article.appendChild(meta);
  article.appendChild(mobileCognitiveCard(
    t("cognitive.section.objective", {}, "Objective interpretation"),
    model.contract.interpreted_goal || model.question || model.title,
  ));
  const planCard = mobileCognitiveCard(
    t("cognitive.section.plan", {}, "Plan"),
    planNodes.length
      ? planNodes.map((node, index) => (
        `${formatUiNumber(index + 1)}. ${node.title} · ${cognitivePlanStateLabel(
          cognitivePlanDisplayState(node, model),
        ).label}`
      )).join("\n")
      : t("cognitive.mobilePlanEmpty", {}, "This task has no displayable Plan Graph nodes."),
  );
  planCard.querySelector(".yunos-mobile-cognitive-card-body")?.classList.add("is-preformatted");
  article.appendChild(planCard);
  article.appendChild(mobileCognitiveCard(
    t("cognitive.mobileProgress", {}, "Progress"),
    `${model.status} · ${t(
      "cognitive.mobileExecutionCount",
      { count: formatUiNumber(executions.length) },
      `${formatUiNumber(executions.length)} tool execution(s) recorded.`,
    )}`,
  ));
  article.appendChild(mobileCognitiveCard(
    t("cognitive.mobileAgent", {}, "Agent"),
    model.run.agent_name
      || model.run.executor_id
      || t("cognitive.nativeAgent", {}, "YunOS native bounded Agent"),
  ));
  article.appendChild(mobileCognitiveCard(
    t("cognitive.mobileOverreach", {}, "Overreach"),
    model.overreachEvents.length
      ? t(
        "cognitive.overreachBlocked",
        { count: formatUiNumber(model.overreachEvents.length) },
        `Blocked ${formatUiNumber(model.overreachEvents.length)} overreach attempt(s)`,
      )
      : t("cognitive.noOverreach", {}, "No overreach detected"),
  ));
  article.appendChild(mobileCognitiveCard(
    t("cognitive.section.verification", {}, "Verification"),
    t(
      "cognitive.mobileVerificationCount",
      { count: formatUiNumber(verifications.length) },
      `${formatUiNumber(verifications.length)} verification record(s) available.`,
    ),
  ));
  article.appendChild(mobileCognitiveCard(
    t("cognitive.mobileWaiting", {}, "Waiting"),
    model.explicitAccepted
      ? t("cognitive.acceptedExplicitly", {}, "Accepted by an independent Acceptance Event")
      : t("cognitive.acceptanceNotRecorded", {}, "User acceptance not recorded"),
  ));
  article.appendChild(el(
    "p",
    "yunos-mobile-cognitive-boundary",
    t(
      "cognitive.mobileReadonlyNotice",
      {},
      "Mobile is read-only. Tasks, authority, high-risk acceptance, and write tools are unavailable here.",
    ),
  ));
  mobileCognitiveContent.appendChild(article);
}

async function openMobileCognitiveWorkspace() {
  const requestSerial = ++mobileCognitiveRequestSerial;
  const currentView = mobileShell?.dataset.mobileView || "home";
  mobileCognitivePreviousView = ["home", "results"].includes(currentView)
    ? currentView
    : "home";
  closeMobileRecentDrawer({ restoreFocus: false });
  setMobileView("cognitive");
  if (mobileCognitiveContent) {
    mobileCognitiveContent.replaceChildren(el(
      "p",
      "yunos-mobile-cognitive-loading",
      t("cognitive.mobileLoading", {}, "Loading cognitive task…"),
    ));
  }
  mobileCognitiveTitle?.focus({ preventScroll: true });
  try {
    const bundle = await loadCognitiveWorkspaceBundle({
      surface: "mobile",
      allowLatest: true,
    });
    if (
      requestSerial !== mobileCognitiveRequestSerial
      || mobileShell?.dataset.mobileView !== "cognitive"
    ) return;
    if (!bundle) {
      renderMobileCognitiveUnavailable();
      return;
    }
    renderMobileCognitiveWorkspace(bundle);
  } catch {
    if (requestSerial !== mobileCognitiveRequestSerial) return;
    renderMobileCognitiveUnavailable();
  }
}

function setMobileView(view) {
  if (!mobileShell) return;
  const nextView = view === "reader"
    ? "reader"
    : (view === "results"
      ? "results"
      : (view === "operational" ? "operational" : (view === "cognitive" ? "cognitive" : "home")));
  mobileShell.dataset.mobileView = nextView;
  if (mobileResultsPage) mobileResultsPage.hidden = nextView !== "results";
  if (mobileConversationReader) mobileConversationReader.hidden = nextView !== "reader";
  if (mobileOperationalPanel) mobileOperationalPanel.hidden = nextView !== "operational";
  if (mobileCognitivePanel) mobileCognitivePanel.hidden = nextView !== "cognitive";
  mobileOperationalButton?.setAttribute(
    "aria-expanded",
    String(nextView === "operational"),
  );
  mobileCognitiveButton?.setAttribute(
    "aria-expanded",
    String(nextView === "cognitive"),
  );
  if (nextView !== "home" && mobileOfflineHome) mobileOfflineHome.hidden = true;
  syncMobileSearchPosition();
}

function syncMobileSearchPosition() {
  if (!mobileShell || !mobileSearchInput) return;
  const visualHeight = window.visualViewport?.height || window.innerHeight;
  const lowHeight = visualHeight <= 560 || window.innerWidth > visualHeight;
  const inputFocused = document.activeElement === mobileSearchInput;
  const shouldPin = (
    (inputFocused && !mobileSearchState.keyboardDismissedWhileFocused)
    || mobileShell.dataset.keyboardOpen === "true"
    || Boolean(mobileSearchInput.value.trim())
    || mobileShell.dataset.mobileView === "results"
    || lowHeight
  );
  mobileShell.dataset.searchPosition = shouldPin ? "top" : "center";
}

function resetMobileSearchResults({ keepQuery = false } = {}) {
  if (mobileSearchState.previewAbortController) mobileSearchState.previewAbortController.abort();
  mobileSearchState.previewAbortController = null;
  mobileSearchState.previewRequestSerial += 1;
  mobileSearchState.groups = [];
  mobileSearchState.totalConversations = 0;
  mobileSearchState.hasMore = false;
  mobileSearchState.nextOffset = 0;
  mobileSearchState.paginationError = false;
  mobileSearchState.autoLoadPaused = false;
  mobileSearchState.expandedConversationId = "";
  mobileSearchState.expandedPreviewKey = "";
  mobileSearchState.resultRefs = [];
  mobileSearchState.searchSnapshotKey = "";
  mobileSearchState.previewCache.clear();
  mobileSearchState.submittedQuery = keepQuery ? mobileSearchState.query : "";
  if (!keepQuery) mobileSearchState.completedFirstPageKey = "";
  mobileSearchResults?.replaceChildren();
  if (mobileResultsSummary) {
    mobileResultsSummary.textContent = t("search.results", {}, "搜索结果");
  }
  setMobileSearchStatus("");
  renderMobileOfflineSearchBanner();
  updateMobileLoadControls();
}

function mobileResultTime(hit = {}) {
  const raw = String(hit.create_time_iso || hit.update_time_iso || hit.latest_time_iso || "").trim();
  if (!raw) return t("date.noDate", {}, "无日期");
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? raw : formatUiShortDate(parsed);
}

function normalizeMobileSearchGroup(item = {}) {
  const hits = Array.isArray(item.hits) ? item.hits : [];
  const bestHit = item.best_hit || hits[0] || item;
  const otherHits = Array.isArray(item.other_hits)
    ? item.other_hits
    : hits.filter((hit) => hit !== bestHit);
  const conversationId = item.conversation_id || bestHit.conversation_id || "";
  const sourceMode = item.source_mode || bestHit.source_mode || "online";
  const matchCount = Math.max(
    Number(item.match_count) || 0,
    1 + otherHits.length,
  );
  return {
    ...item,
    conversation_id: conversationId,
    title: item.title
      || bestHit.title
      || t("search.untitledConversation", {}, "未命名会话"),
    source_mode: sourceMode,
    best_hit: {
      ...bestHit,
      conversation_id: conversationId,
      source_mode: bestHit.source_mode || sourceMode,
    },
    other_hits: otherHits.map((hit) => ({
      ...hit,
      conversation_id: conversationId,
      source_mode: hit.source_mode || sourceMode,
    })),
    match_count: matchCount,
    other_match_count: Math.max(Number(item.other_match_count) || 0, matchCount - 1),
  };
}

function mobileHitIsSemantic(hit, query) {
  if (hit?.match_type === "semantic" || hit?.semantic_match === true) return true;
  if (hit?.match_type) return false;
  return false;
}

function mobileHitKey(hit = {}) {
  return JSON.stringify([
    String(hit.conversation_id || ""),
    String(hit.message_id || ""),
  ]);
}

function mobileHitBlock(hit, query, { secondary = false, previewId = "", hitKey = "" } = {}) {
  const block = el("button", "yunos-mobile-result-body");
  block.type = "button";
  block.dataset.mobileHitKey = hitKey;
  block.setAttribute("aria-expanded", "false");
  if (previewId) block.setAttribute("aria-controls", previewId);
  block.setAttribute(
    "aria-label",
    t(
      secondary ? "aria.expandSameConversationMatch" : "aria.expandMatchedBody",
      { snippet: cleanMobileSearchSnippet(hit?.snippet) || t("search.noMatchingBody", {}, "无匹配正文") },
      `${secondary ? "展开同一会话中的匹配" : "展开命中正文"}：${cleanMobileSearchSnippet(hit?.snippet) || "无匹配正文"}`,
    ),
  );
  const snippet = el("p", "yunos-mobile-result-snippet");
  const snippetText = hit?.source_mode === "cached-only"
    ? (String(hit?.snippet || "") || t("search.noMatchingBody", {}, "无匹配正文"))
    : (cleanMobileSearchSnippet(hit?.snippet)
      || t("search.noMatchingBody", {}, "无匹配正文"));
  if (hit?.source_mode === "cached-only" && Array.isArray(hit?.match_ranges)) {
    appendHighlightedRanges(snippet, snippetText, hit.match_ranges);
  } else {
    appendHighlighted(snippet, snippetText, query);
  }
  block.appendChild(snippet);
  if (mobileHitIsSemantic(hit, query)) {
    block.appendChild(el(
      "span",
      "yunos-mobile-related-match",
      t("search.relatedMatch", {}, "相关匹配"),
    ));
  } else if (hit?.source_mode === "cached-only") {
    block.appendChild(el(
      "span",
      "yunos-mobile-cached-match",
      t("offline.cachedCopy", {}, "缓存副本"),
    ));
  }
  return block;
}

function mobileResultSourceButton(group, hit, { secondary = false, index = 0 } = {}) {
  const source = el("button", "yunos-mobile-result-source");
  source.type = "button";
  const copy = el("span", "yunos-mobile-result-source-copy");
  copy.append(
    el(
      "span",
      "yunos-mobile-result-title",
      secondary
        ? t(
          "search.sameConversationMatch",
          { index: formatUiNumber(index + 1) },
          `同一会话中的匹配 ${formatUiNumber(index + 1)}`,
        )
        : (group.title || t("search.untitledConversation", {}, "未命名会话")),
    ),
    el("time", "yunos-mobile-result-date", mobileResultTime(hit)),
  );
  source.append(copy, el("span", "yunos-mobile-result-arrow", "›"));
  source.setAttribute(
    "aria-label",
    t(
      "aria.openConversationAtMatch",
      {
        title: group.title || t("search.untitledConversation", {}, "未命名会话"),
        time: mobileResultTime(hit),
      },
      `打开完整会话 ${group.title || "未命名会话"}，定位到 ${mobileResultTime(hit)} 的命中消息`,
    ),
  );
  return source;
}

function formatMobileMessageTime(value) {
  const date = new Date(value || "");
  if (Number.isNaN(date.getTime())) return t("date.timeUnknown", {}, "时间未知");
  return formatUiDateTime(date, {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function mobileMessageIsUser(message = {}) {
  return String(message.role || "").toLowerCase() === "user";
}

function formatMobileCacheTime(value, { prefix = true } = {}) {
  const date = new Date(Number(value) || value || "");
  if (Number.isNaN(date.getTime())) {
    return t("offline.cacheTimeUnknown", {}, "缓存时间未知");
  }
  const formatted = formatUiDateTime(date, {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  return prefix
    ? t("offline.cachedAt", { time: formatted }, `缓存于 ${formatted}`)
    : formatted;
}

function cachedMessageForMobile(message, anchorMessageId = "") {
  return {
    conversation_id: message.conversationId,
    message_id: message.messageId,
    role: message.role,
    text: message.text,
    create_time_iso: message.messageTime,
    position: message.position,
    is_focus: Boolean(
      message.isFocus
      || (anchorMessageId && message.messageId === anchorMessageId)
    ),
    offline_trace_unavailable: true,
  };
}

let mobileCacheToastTimer = 0;
let mobileOfflineCatalogRequestSerial = 0;

function showMobileCacheToast(message) {
  if (!mobileCacheToast) return;
  window.clearTimeout(mobileCacheToastTimer);
  mobileCacheToast.textContent = message;
  mobileCacheToast.hidden = false;
  mobileCacheToastTimer = window.setTimeout(() => {
    mobileCacheToast.hidden = true;
  }, 5000);
}

function reportMobileCacheWriteFailure(error) {
  console.warn("YunOS offline text cache write failed; online reading continues.", {
    code: error?.code || error?.name || "cache_write_failed",
    message: error?.message || "unknown",
  });
  showMobileCacheToast(t(
    "offline.cacheSaveFailed",
    {},
    "当前内容未能保存为离线副本",
  ));
}

function normalizedCacheMessages(messages = []) {
  return messages.map((message) => ({
    conversationId: message.conversation_id,
    messageId: message.message_id,
    role: message.role,
    text: message.text,
    messageTime: message.create_time_iso,
    position: message.position,
  }));
}

async function saveMobileMessageResponse({
  data,
  direction,
  requestCursor = "",
  expectedMessageCount = 0,
}) {
  const cache = await mobilePrivateCacheReady;
  if (!cache || !isMobileSearchSurfaceActive()) return null;
  const messages = Array.isArray(data?.messages) ? data.messages : [];
  if (!messages.length) return null;
  const conversationId = String(
    data.conversation_id
    || data.conversation?.conversation_id
    || messages[0]?.conversation_id
    || "",
  ).trim();
  const anchorMessageId = String(
    data.focus_message_id
    || data.focus?.message_id
    || mobileConversationState.focusMessageId
    || "",
  ).trim();
  const windowAnchorMessageId = messages.some(
    (message) => String(message?.message_id || "").trim() === anchorMessageId,
  )
    ? anchorMessageId
    : "";
  const cursors = data.cursors || {};
  const saved = await cache.saveMessageWindow({
    conversationId,
    conversationTitle: data.conversation_title || data.conversation?.title || "",
    anchorMessageId: windowAnchorMessageId,
    direction,
    requestCursor,
    cursorBefore: cursors.before || "",
    cursorAfter: cursors.after || "",
    reachedStart: direction === "preview"
      ? "unknown"
      : (typeof data.has_before === "boolean" ? !data.has_before : "unknown"),
    reachedEnd: direction === "preview"
      ? "unknown"
      : (typeof data.has_after === "boolean" ? !data.has_after : "unknown"),
    expectedMessageCount: expectedMessageCount || messages.length,
    messages: normalizedCacheMessages(messages),
  });
  if (
    mobileConversationState.conversationId === conversationId
    && mobileShell?.dataset.mobileView === "reader"
  ) {
    if (!mobileConversationState.cachedSegmentKeys.includes(saved.segment.segmentKey)) {
      mobileConversationState.cachedSegmentKeys.push(saved.segment.segmentKey);
    }
    await protectActiveMobileReaderCache(cache).catch(() => {});
  }
  return saved;
}

function cacheFallbackMessage(result, label) {
  if (result?.status === "expired") {
    return t("offline.cacheExpired", {}, "缓存已过期，请连接主机重新加载");
  }
  if (result?.status === "incomplete") {
    return t("offline.cacheIncomplete", { label }, `${label}缓存不完整`);
  }
  return t(
    "offline.notCached",
    { label },
    `${label}未缓存，连接主机后可重新加载`,
  );
}

function mobilePreviewMessageElement(message, query) {
  const isUser = mobileMessageIsUser(message);
  const row = el(
    "article",
    `yunos-mobile-preview-message ${isUser ? "is-user" : "is-assistant"}${message.is_focus ? " is-focus" : ""}`,
  );
  row.setAttribute("role", "listitem");
  const label = el("div", "yunos-mobile-message-label");
  label.append(
    el("span", "", isUser ? t("common.you", {}, "你") : t("nav.ai", {}, "AI")),
    el("time", "yunos-mobile-message-time", formatMobileMessageTime(message.create_time_iso)),
  );
  if (message.is_focus) {
    label.appendChild(el(
      "span",
      "yunos-mobile-hit-badge",
      t("search.match", {}, "命中"),
    ));
  }
  const bubble = el("div", "yunos-mobile-message-bubble");
  appendHighlighted(bubble, message.text || "", query);
  row.append(label, bubble);
  return row;
}

function renderMobileContextPreview(preview, entry, query) {
  if (!preview) return;
  preview.replaceChildren();
  if (!entry || entry.status === "loading") {
    preview.appendChild(el(
      "p",
      "yunos-mobile-preview-status",
      t("mobile.loadingNearbyMessages", {}, "正在加载附近消息…"),
    ));
    return;
  }
  if (entry.status === "error") {
    const status = el("div", "yunos-mobile-preview-status is-error");
    status.appendChild(el(
      "span",
      "",
      entry.error || t("error.nearbyMessagesFailed", {}, "附近消息加载失败。"),
    ));
    const disclosure = technicalDetailsElement(
      entry.technicalDetail,
      "yunos-mobile-reader-error-details",
    );
    if (disclosure) status.appendChild(disclosure);
    const retry = el(
      "button",
      "yunos-mobile-reader-retry",
      t("common.reload", {}, "重新加载"),
    );
    retry.type = "button";
    retry.addEventListener("click", () => {
      loadMobileContextPreview(entry.group, entry.hit, entry.key).catch(() => {});
    });
    status.appendChild(retry);
    preview.appendChild(status);
    return;
  }
  if (entry.offline) {
    const offline = el("div", "yunos-mobile-preview-offline");
    offline.append(
      el(
        "strong",
        "",
        t("offline.showingLocalCopy", {}, "当前显示本机缓存副本"),
      ),
      el("span", "", formatMobileCacheTime(entry.cachedAt)),
      el("span", "", t("offline.mayLagHost", {}, "内容可能落后于主机")),
    );
    const retry = el(
      "button",
      "yunos-mobile-reader-retry",
      t("common.reload", {}, "重新加载"),
    );
    retry.type = "button";
    retry.addEventListener("click", () => {
      loadMobileContextPreview(entry.group, entry.hit, entry.key).catch(() => {});
    });
    offline.appendChild(retry);
    preview.appendChild(offline);
  }
  const messages = Array.isArray(entry.data?.messages) ? entry.data.messages : [];
  if (!messages.length) {
    preview.appendChild(el(
      "p",
      "yunos-mobile-preview-status",
      t("mobile.noNearbyMessages", {}, "没有可显示的附近消息。"),
    ));
    return;
  }
  messages.forEach((message) => preview.appendChild(mobilePreviewMessageElement(message, query)));
}

function syncMobileExpandedPreview() {
  const query = mobileSearchState.submittedQuery || mobileSearchState.query;
  mobileSearchResults?.querySelectorAll(".yunos-mobile-result-unit").forEach((unit) => {
    const key = unit.dataset.mobileHitKey || "";
    const expanded = Boolean(key && key === mobileSearchState.expandedPreviewKey);
    const body = unit.querySelector(".yunos-mobile-result-body");
    const preview = unit.querySelector(".yunos-mobile-context-preview");
    if (body) body.setAttribute("aria-expanded", String(expanded));
    if (!preview) return;
    preview.hidden = !expanded;
    if (expanded) renderMobileContextPreview(preview, mobileSearchState.previewCache.get(key), query);
  });
}

async function loadMobileContextPreview(group, hit, key = "") {
  key = key || mobileHitKey(hit);
  if (!hit?.conversation_id || !hit?.message_id) {
    mobileSearchState.previewCache.set(key, {
      status: "error",
      error: t(
        "search.resultMissingLocatableAnchor",
        {},
        "该结果缺少可定位的消息锚点。",
      ),
      group,
      hit,
      key,
    });
    syncMobileExpandedPreview();
    return;
  }
  if (hit.source_mode === "cached-only") {
    mobileSearchState.previewCache.set(key, { status: "loading", group, hit, key });
    syncMobileExpandedPreview();
    const cache = await mobilePrivateCacheReady;
    const cached = cache
      ? await cache.getCachedMessageContext({
        conversationId: hit.conversation_id,
        anchorMessageId: hit.message_id,
        expectedMessageCount: 5,
      }).catch(() => ({ status: "incomplete" }))
      : { status: "missing" };
    if (cached.status === "ready") {
      mobileSearchState.previewCache.set(key, {
        status: "ready",
        offline: true,
        cachedAt: cached.cachedAt,
        data: {
          conversation_id: cached.conversationId,
          conversation_title: cached.conversationTitle,
          focus_message_id: cached.anchorMessageId,
          messages: cached.messages.map((message) => (
            cachedMessageForMobile(message, cached.anchorMessageId)
          )),
        },
        group,
        hit,
        key,
      });
    } else {
      mobileSearchState.previewCache.set(key, {
        status: "error",
        error: cached.status === "incomplete"
          ? t(
            "offline.previewIncompleteNoStitch",
            {},
            "缓存预览不完整，未从其他未知分段拼接",
          )
          : cacheFallbackMessage(
            cached,
            t("mobile.nearbyMessages", {}, "附近消息"),
          ),
        group,
        hit,
        key,
      });
    }
    syncMobileExpandedPreview();
    return;
  }
  if (mobileSearchState.previewAbortController) mobileSearchState.previewAbortController.abort();
  const controller = new AbortController();
  const serial = ++mobileSearchState.previewRequestSerial;
  mobileSearchState.previewAbortController = controller;
  mobileSearchState.previewCache.set(key, { status: "loading", group, hit, key });
  syncMobileExpandedPreview();
  const params = new URLSearchParams({
    conversation_id: hit.conversation_id,
    message_id: hit.message_id,
    window: "2",
  });
  try {
    const data = await messageApi(
      `/api/message-context?${params.toString()}`,
      { signal: controller.signal },
    );
    if (serial !== mobileSearchState.previewRequestSerial) return;
    mobileSearchState.previewCache.set(key, { status: "ready", data, group, hit, key });
    saveMobileMessageResponse({
      data,
      direction: "preview",
      expectedMessageCount: 5,
    }).catch(reportMobileCacheWriteFailure);
  } catch (error) {
    if (error?.name === "AbortError" || serial !== mobileSearchState.previewRequestSerial) return;
    if (allowsPrivateCacheFallback(error)) {
      const cache = await mobilePrivateCacheReady;
      const cached = cache
        ? await cache.getCachedMessageContext({
          conversationId: hit.conversation_id,
          anchorMessageId: hit.message_id,
          expectedMessageCount: 5,
        }).catch(() => ({ status: "incomplete" }))
        : { status: "missing" };
      if (serial !== mobileSearchState.previewRequestSerial) return;
      if (cached.status === "ready") {
        mobileSearchState.previewCache.set(key, {
          status: "ready",
          offline: true,
          cachedAt: cached.cachedAt,
          data: {
            conversation_id: cached.conversationId,
            conversation_title: cached.conversationTitle,
            focus_message_id: cached.anchorMessageId,
            messages: cached.messages.map((message) => (
              cachedMessageForMobile(message, cached.anchorMessageId)
            )),
          },
          group,
          hit,
          key,
        });
        return;
      }
      mobileSearchState.previewCache.set(key, {
        status: "error",
        error: cacheFallbackMessage(
          cached,
          t("mobile.nearbyMessages", {}, "附近消息"),
        ),
        group,
        hit,
        key,
      });
      return;
    }
    mobileSearchState.previewCache.set(key, {
      status: "error",
      error: t("error.nearbyMessagesFailed", {}, "附近消息加载失败。"),
      technicalDetail: error?.technicalDetail
        || error?.message
        || "NEARBY_MESSAGES_LOAD_FAILED",
      group,
      hit,
      key,
    });
  } finally {
    if (serial === mobileSearchState.previewRequestSerial) {
      mobileSearchState.previewAbortController = null;
      syncMobileExpandedPreview();
    }
  }
}

function toggleMobileContextPreview(group, hit, body) {
  const key = mobileHitKey(hit);
  const nextKey = mobileSearchState.expandedPreviewKey === key ? "" : key;
  const beforeTop = body?.getBoundingClientRect().top ?? 0;
  const beforeScrollTop = mobileResultsPage?.scrollTop || 0;
  if (!nextKey && mobileSearchState.previewAbortController) {
    mobileSearchState.previewAbortController.abort();
    mobileSearchState.previewAbortController = null;
    mobileSearchState.previewRequestSerial += 1;
  }
  mobileSearchState.expandedPreviewKey = nextKey;
  syncMobileExpandedPreview();
  scheduleMobileRestoreStateSave({ interaction: true });
  if (body && mobileResultsPage) {
    const afterTop = body.getBoundingClientRect().top;
    mobileResultsPage.scrollTop = Math.max(0, beforeScrollTop + afterTop - beforeTop);
  }
  if (!nextKey) return;
  const cached = mobileSearchState.previewCache.get(key);
  if (!cached || cached.status === "error") {
    loadMobileContextPreview(group, hit, key).catch(() => {});
  }
}

function mobileResultHitUnit(group, hit, query, { secondary = false, index = 0, idSuffix = "" } = {}) {
  const key = mobileHitKey(hit);
  const unit = el("section", "yunos-mobile-result-unit");
  unit.dataset.mobileHitKey = key;
  const previewId = `mobile-context-preview-${idSuffix}`;
  const body = mobileHitBlock(hit, query, { secondary, previewId, hitKey: key });
  const preview = el("div", "yunos-mobile-context-preview");
  preview.id = previewId;
  preview.hidden = true;
  preview.setAttribute("role", "list");
  preview.setAttribute(
    "aria-label",
    t("aria.nearbyChat", {}, "命中消息附近的聊天记录"),
  );
  const source = mobileResultSourceButton(group, hit, { secondary, index });
  body.addEventListener("click", () => toggleMobileContextPreview(group, hit, body));
  source.addEventListener("click", () => openMobileConversationReader(group, hit));
  unit.append(body, preview, source);
  return unit;
}

function setExpandedMobileConversation(conversationId = "", { persist = true } = {}) {
  mobileSearchState.expandedConversationId = conversationId;
  mobileSearchResults?.querySelectorAll(".yunos-mobile-result").forEach((result) => {
    const expanded = Boolean(conversationId && result.dataset.conversationId === conversationId);
    result.classList.toggle("is-expanded", expanded);
    const buttonElement = result.querySelector(".yunos-mobile-more-matches");
    const matches = result.querySelector(".yunos-mobile-other-matches");
    if (buttonElement) buttonElement.setAttribute("aria-expanded", String(expanded));
    if (matches) matches.hidden = !expanded;
  });
  if (persist) scheduleMobileRestoreStateSave({ interaction: true });
}

function mobileSearchGroupElement(group, query, index) {
  const article = el("article", "yunos-mobile-result yunos-mobile-result-item");
  article.setAttribute("role", "listitem");
  article.dataset.conversationId = group.conversation_id;
  article.appendChild(mobileResultHitUnit(group, group.best_hit, query, { idSuffix: `${index}-best` }));

  if (group.other_match_count > 0) {
    const matchesId = `mobile-other-matches-${index}`;
    const toggle = el(
      "button",
      "yunos-mobile-more-matches yunos-mobile-result-more-toggle",
      group.source_mode === "cached-only"
        ? t(
          "search.moreCachedMatches",
          { count: formatUiNumber(group.other_match_count) },
          `另有 ${formatUiNumber(group.other_match_count)} 处缓存匹配`,
        )
        : t(
          "search.moreMatches",
          { count: formatUiNumber(group.other_match_count) },
          `另有 ${formatUiNumber(group.other_match_count)} 处匹配`,
        ),
    );
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-controls", matchesId);
    const matches = el("div", "yunos-mobile-other-matches yunos-mobile-result-matches");
    matches.id = matchesId;
    matches.hidden = true;
    group.other_hits.forEach((hit, hitIndex) => matches.appendChild(mobileResultHitUnit(
      group,
      hit,
      query,
      { secondary: true, index: hitIndex, idSuffix: `${index}-${hitIndex}` },
    )));
    toggle.addEventListener("click", () => {
      const nextId = mobileSearchState.expandedConversationId === group.conversation_id
        ? ""
        : group.conversation_id;
      setExpandedMobileConversation(nextId);
    });
    article.append(toggle, matches);
  }
  return article;
}

function renderMobileSearchResults() {
  if (!mobileSearchResults) return;
  mobileSearchResults.replaceChildren();
  const query = mobileSearchState.submittedQuery || mobileSearchState.query;
  if (!mobileSearchState.groups.length) {
    const empty = el("div", "yunos-mobile-empty-result yunos-mobile-results-empty");
    empty.append(
      el(
        "p",
        "yunos-mobile-empty-title",
        mobileSearchState.sourceMode === "cached-only"
          ? t("offline.noCachedResults", {}, "已缓存内容中没有匹配结果")
          : t("search.noResults", {}, "没有找到匹配结果"),
      ),
      el(
        "p",
        "yunos-mobile-empty-copy",
        mobileSearchState.sourceMode === "cached-only"
          ? t(
            "offline.incompleteRangeRecover",
            {},
            "当前范围可能不完整；恢复网络后可搜索全部主机内容。",
          )
          : t("search.tryAnotherKeyword", {}, "换一个关键词试试。"),
      ),
    );
    mobileSearchResults.appendChild(empty);
  } else {
    mobileSearchState.groups.forEach((group, index) => {
      mobileSearchResults.appendChild(mobileSearchGroupElement(group, query, index));
    });
  }
  if (mobileResultsSummary) {
    const count = formatUiNumber(mobileSearchState.totalConversations);
    mobileResultsSummary.textContent = t(
      "mobile.conversationCount",
      { count },
      `${count} 个会话`,
    );
  }
  mobileSearchState.resultRefs = currentMobileResultRefs();
  if (mobileSearchState.expandedConversationId) {
    setExpandedMobileConversation(mobileSearchState.expandedConversationId, { persist: false });
  }
  if (mobileSearchState.expandedPreviewKey) syncMobileExpandedPreview();
  if (mobileSearchSentinel) mobileSearchResults.appendChild(mobileSearchSentinel);
  if (mobileLoadMore) mobileSearchResults.appendChild(mobileLoadMore);
  renderMobileOfflineSearchBanner();
  updateMobileLoadControls();
}

function updateMobileLoadControls() {
  if (!mobileLoadMore || !mobileSearchSentinel) return;
  const count = mobileSearchState.groups.length;
  const showRetry = mobileSearchState.paginationError;
  const reachedAutoLimit = count >= MOBILE_SEARCH_AUTO_LIMIT;
  const showManualMore = mobileSearchState.hasMore && reachedAutoLimit;
  mobileLoadMore.hidden = !(showRetry || showManualMore);
  mobileLoadMore.textContent = showRetry
    ? t("common.reload", {}, "重新加载")
    : t("common.loadMore", {}, "加载更多");
  mobileLoadMore.setAttribute(
    "aria-label",
    showRetry
      ? t("aria.reloadSearchResults", {}, "重新加载搜索结果")
      : t("aria.loadMoreSearchResults", {}, "加载更多搜索结果"),
  );
  mobileSearchSentinel.hidden = !(
    mobileSearchState.hasMore
    && count > 0
    && count < MOBILE_SEARCH_AUTO_LIMIT
    && !mobileSearchState.autoLoadPaused
  );
  if (!mobileSearchState.loading && count > 0 && !mobileSearchState.hasMore) {
    setMobileSearchStatus(
      mobileSearchState.sourceMode === "cached-only"
        ? t("offline.allCachedResultsShown", {}, "已显示全部缓存结果")
        : t("search.allResultsShown", {}, "已显示全部结果"),
    );
  } else if (!mobileSearchState.loading && showManualMore) {
    setMobileSearchStatus(t(
      "search.autoLoadedPause",
      { count: formatUiNumber(count) },
      `已自动加载 ${formatUiNumber(count)} 条，继续加载由你决定。`,
    ));
  }
}

function mobileSearchUrl(query, offset) {
  const params = new URLSearchParams({
    q: query,
    scope: "all",
    aggregate: "conversation",
    sort: mobileSearchState.sort,
    limit: String(MOBILE_SEARCH_PAGE_SIZE),
    offset: String(offset),
  });
  return `/api/search?${params.toString()}`;
}

function applyMobileSearchData(data, {
  reset,
  firstPageKey,
  sourceMode,
}) {
  const nextGroups = (Array.isArray(data.items) ? data.items : []).map(
    normalizeMobileSearchGroup,
  );
  if (reset) {
    mobileSearchState.groups = nextGroups;
  } else {
    const known = new Set(mobileSearchState.groups.map((group) => group.conversation_id));
    mobileSearchState.groups.push(
      ...nextGroups.filter((group) => !known.has(group.conversation_id)),
    );
  }
  mobileSearchState.sourceMode = sourceMode;
  mobileSearchState.totalConversations = Number(
    data.total_conversations ?? data.totalConversations,
  ) || mobileSearchState.groups.length;
  mobileSearchState.hasMore = Boolean(data.has_more ?? data.hasMore);
  const nextOffset = data.next_offset ?? data.nextOffset;
  mobileSearchState.nextOffset = Number.isFinite(Number(nextOffset))
    ? Number(nextOffset)
    : mobileSearchState.groups.length;
  mobileSearchState.completedFirstPageKey = firstPageKey;
  mobileSearchState.cacheCoverage = data.coverage || mobileSearchState.cacheCoverage;
  mobileSearchState.resultRefs = Array.isArray(data.resultRefs)
    ? data.resultRefs
    : currentMobileResultRefs();
  mobileSearchState.loading = false;
  mobileSearchState.abortController = null;
  mobileSearchState.activeRequestKey = "";
  renderMobileSearchResults();
}

async function runMobileSearch({
  reset = true,
  force = false,
  forceCached = false,
  forceOnline = false,
  restoreLimit = 0,
} = {}) {
  if (!mobileSearchInput || !mobileShell) return;
  clearMobileSearchDebounce();
  const query = mobileSearchInput.value.trim();
  mobileSearchState.query = query;
  const visibleCharacters = mobileVisibleCharacterCount(query);
  if (visibleCharacters < 2) {
    abortMobileSearchRequest({ invalidate: true });
    if (visibleCharacters === 1) {
      setMobileSearchHint(t("search.enterOneMoreCharacter", {}, "再输入一个字开始搜索"));
    }
    return;
  }

  const firstPageKey = mobileFirstPageKey(query);
  const offset = reset ? 0 : mobileSearchState.nextOffset;
  const useCached = !forceOnline && (
    forceCached
    || !isMobileHostAvailable()
    || mobileSearchState.sourceMode === "cached-only"
  );
  const sourceMode = useCached ? "cached-only" : "online";
  const requestKey = `${sourceMode}\u0000${firstPageKey}\u0000${offset}`;
  if (!force) {
    if (mobileSearchState.activeRequestKey === requestKey) return;
    if (reset && mobileSearchState.completedFirstPageKey === firstPageKey) return;
  }

  abortMobileSearchRequest();
  const requestSerial = ++mobileSearchState.requestSerial;
  const controller = new AbortController();
  mobileSearchState.abortController = controller;
  mobileSearchState.activeRequestKey = requestKey;
  mobileSearchState.loading = true;
  mobileSearchState.paginationError = false;
  mobileSearchState.autoLoadPaused = false;
  mobileSearchState.submittedQuery = query;
  mobileSearchState.sourceMode = sourceMode;
  setMobileSearchHint("");
  setMobileView("results");
  if (reset) {
    mobileSearchState.groups = [];
    mobileSearchState.totalConversations = 0;
    mobileSearchState.hasMore = false;
    mobileSearchState.nextOffset = 0;
    mobileSearchState.expandedConversationId = "";
    mobileSearchState.expandedPreviewKey = "";
    mobileSearchState.previewCache.clear();
    mobileSearchResults.replaceChildren();
    if (mobileResultsSummary) {
      mobileResultsSummary.textContent = t("search.results", {}, "搜索结果");
    }
  }
  renderMobileOfflineSearchBanner();
  setMobileSearchStatus(
    mobileSearchState.restoring && reset
      ? t("mobile.restoringAndRefreshing", {}, "正在恢复并刷新")
      : (
        useCached
          ? (reset
            ? t("offline.searchingCached", {}, "正在搜索已缓存内容…")
            : t("offline.loadingMoreCached", {}, "正在加载更多缓存结果…"))
          : (reset
            ? t("search.searching", {}, "正在搜索…")
            : t("common.loadingMore", {}, "正在加载更多…"))
      ),
    "loading",
  );
  updateMobileLoadControls();

  try {
    let data;
    if (useCached) {
      const cache = await mobilePrivateCacheReady;
      if (!cache) {
        throw new Error(t("error.privateCacheUnavailable", {}, "本机私人缓存不可用"));
      }
      data = await cache.searchCachedMessages({
        query,
        sortMode: mobileSearchState.sort,
        offset,
        limit: reset
          ? Math.max(MOBILE_SEARCH_PAGE_SIZE, Number(restoreLimit) || 0)
          : MOBILE_SEARCH_PAGE_SIZE,
        signal: controller.signal,
      });
      cache.saveSearchSnapshot({
        query,
        sortMode: mobileSearchState.sort,
        resultRefs: data.resultRefs,
      }).then((snapshot) => {
        mobileSearchState.searchSnapshotKey = snapshot.snapshotKey;
      }).catch((error) => {
        console.warn("YunOS cached search snapshot write failed.", {
          code: error?.code || error?.name || "snapshot_write_failed",
        });
      });
    } else {
      data = await api(mobileSearchUrl(query, offset), { signal: controller.signal });
    }
    if (requestSerial !== mobileSearchState.requestSerial) return;
    applyMobileSearchData(data, { reset, firstPageKey, sourceMode });
    if (!mobileSearchState.restoring) recordMobileRecentSearch(query);
    if (!mobileSearchState.groups.length) {
      setMobileSearchStatus(
        sourceMode === "cached-only"
          ? t("offline.noCachedResults", {}, "已缓存内容中没有匹配结果")
          : t("search.noMatches", {}, "没有匹配结果"),
      );
    }
    else if (mobileSearchState.hasMore && mobileSearchState.groups.length < MOBILE_SEARCH_AUTO_LIMIT) {
      const count = formatUiNumber(mobileSearchState.groups.length);
      setMobileSearchStatus(
        sourceMode === "cached-only"
          ? t(
            "offline.cachedConversationShown",
            { count },
            `已显示 ${count} 个缓存会话`,
          )
          : t("mobile.conversationsShown", { count }, `已显示 ${count} 个会话`),
      );
    }
    scheduleMobileRestoreStateSave({ interaction: false });
  } catch (error) {
    if (error?.name === "AbortError" || requestSerial !== mobileSearchState.requestSerial) return;
    if (reset && !useCached && allowsPrivateCacheFallback(error)) {
      mobileSearchState.loading = false;
      mobileSearchState.abortController = null;
      mobileSearchState.activeRequestKey = "";
      mobileSearchState.completedFirstPageKey = "";
      await runMobileSearch({
        reset,
        force: true,
        forceCached: true,
        restoreLimit,
      });
      return;
    }
    mobileSearchState.loading = false;
    mobileSearchState.abortController = null;
    mobileSearchState.activeRequestKey = "";
    mobileSearchState.paginationError = true;
    mobileSearchState.autoLoadPaused = true;
    setMobileSearchStatus(
      useCached
        ? (reset
          ? t("error.cachedSearchFailed", {}, "缓存搜索失败，请手动重试。")
          : t("error.cachedLoadMoreFailed", {}, "缓存结果加载失败，请手动重试。"))
        : (reset
          ? t("error.searchFailed", {}, "搜索失败，请手动重试。")
          : t("error.loadMoreFailed", {}, "加载失败，请手动重试。")),
      "error",
    );
    updateMobileLoadControls();
  }
}

function scheduleMobileSearch() {
  clearMobileSearchDebounce();
  mobileSearchState.debounceTimer = window.setTimeout(() => {
    mobileSearchState.debounceTimer = 0;
    runMobileSearch({ reset: true }).catch(() => {});
  }, MOBILE_SEARCH_DEBOUNCE_MS);
}

function loadNextMobileSearchPage({ force = false } = {}) {
  if (
    mobileSearchState.loading
    || !mobileSearchState.hasMore
    || (!force && mobileSearchState.groups.length >= MOBILE_SEARCH_AUTO_LIMIT)
  ) return;
  runMobileSearch({ reset: false, force }).catch(() => {});
}

function mobileConversationKey(conversationId, focusMessageId) {
  return JSON.stringify([String(conversationId || ""), String(focusMessageId || "")]);
}

function mobileConversationUrl(direction = "initial") {
  const params = new URLSearchParams({
    conversation_id: mobileConversationState.conversationId,
    limit: String(MOBILE_READER_PAGE_SIZE),
  });
  if (direction === "before") params.set("before", mobileConversationState.beforeCursor);
  else if (direction === "after") params.set("after", mobileConversationState.afterCursor);
  else params.set("anchor_message_id", mobileConversationState.focusMessageId);
  return `/api/conversation-messages?${params.toString()}`;
}

function setMobileReaderTopbarVisible(visible, { remember = true } = {}) {
  const nextVisible = visible !== false;
  if (mobileConversationReader) {
    mobileConversationReader.dataset.topbarVisible = String(nextVisible);
  }
  if (mobileReaderTopbar) mobileReaderTopbar.hidden = !nextVisible;
  if (mobileReaderRestoreTopbar) mobileReaderRestoreTopbar.hidden = nextVisible;
  if (mobileReaderHideTopbar) {
    mobileReaderHideTopbar.setAttribute("aria-expanded", String(nextVisible));
  }
  mobileReaderRestoreTopbar?.setAttribute("aria-expanded", String(!nextVisible));
  if (remember && mobileConversationState.conversationId) {
    mobileConversationState.topbarVisibility.set(
      mobileConversationState.conversationId,
      nextVisible,
    );
  }
}

function toggleMobileReaderTrace(messageId, row, trace) {
  const selection = window.getSelection?.();
  if (selection && !selection.isCollapsed && selection.toString().trim()) return;
  const expanded = !row.classList.contains("is-trace-expanded");
  row.classList.toggle("is-trace-expanded", expanded);
  trace.hidden = !expanded;
  row.querySelector(".yunos-mobile-reader-bubble")?.setAttribute("aria-expanded", String(expanded));
  if (expanded) mobileConversationState.expandedTraceIds.add(messageId);
  else mobileConversationState.expandedTraceIds.delete(messageId);
}

function mobileReaderMessageElement(message) {
  const isUser = mobileMessageIsUser(message);
  const messageId = String(message.message_id || "");
  const traceExpanded = mobileConversationState.expandedTraceIds.has(messageId);
  const row = el(
    "article",
    `yunos-mobile-reader-message ${isUser ? "is-user" : "is-assistant"}${message.is_focus ? " is-focus" : ""}${traceExpanded ? " is-trace-expanded" : ""}`,
  );
  row.dataset.messageId = messageId;
  row.dataset.messagePosition = String(message.position ?? "");
  row.setAttribute("role", "listitem");

  const time = el("time", "yunos-mobile-reader-time", formatMobileMessageTime(message.create_time_iso));
  const bubble = el("div", "yunos-mobile-reader-bubble");
  bubble.setAttribute("role", "button");
  bubble.setAttribute("tabindex", "0");
  bubble.setAttribute("aria-expanded", String(traceExpanded));
  bubble.setAttribute(
    "aria-label",
    t(
      "aria.messageTrace",
      { role: isUser ? t("common.user", {}, "用户") : t("nav.ai", {}, "AI") },
      `${isUser ? "用户" : "AI"} 消息，点击显示追溯信息`,
    ),
  );
  const messageText = el("div", "yunos-mobile-reader-text");
  appendHighlighted(messageText, message.text || "", mobileConversationState.query);
  bubble.appendChild(messageText);
  if (message.is_focus) {
    bubble.appendChild(el(
      "span",
      "yunos-mobile-hit-badge",
      t("search.match", {}, "命中"),
    ));
  }

  const trace = el("div", "yunos-mobile-reader-trace");
  trace.hidden = !traceExpanded;
  if (mobileConversationState.offline || message.offline_trace_unavailable) {
    trace.appendChild(el(
      "span",
      "",
      t(
        "offline.traceUnavailable",
        {},
        "离线副本未保存模型、内部编号和追溯信息",
      ),
    ));
  } else {
    const model = message.model_trace?.resolved_model_slug
      || message.model_slug
      || message.default_model_slug
      || mobileConversationState.conversation?.default_model_slug
      || t("common.notRecorded", {}, "未记录");
    const position = Number(message.position);
    const ordinal = message.ordinal;
    const displayPosition = Number.isFinite(position)
      ? formatUiNumber(position)
      : (Number.isFinite(Number(ordinal))
        ? formatUiNumber(Number(ordinal) + 1)
        : "-");
    trace.append(
      el("span", "", t("ai.modelValue", { model }, `模型 ${model}`)),
      el(
        "span",
        "",
        t(
          "mobile.messagePosition",
          { position: displayPosition },
          `消息 #${displayPosition}`,
        ),
      ),
      el(
        "span",
        "",
        t(
          "mobile.messageId",
          { id: messageId || t("common.notRecorded", {}, "未记录") },
          `ID ${messageId || "未记录"}`,
        ),
      ),
    );
  }
  bubble.addEventListener("click", () => toggleMobileReaderTrace(messageId, row, trace));
  bubble.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    toggleMobileReaderTrace(messageId, row, trace);
  });
  row.append(time, bubble, trace);
  return row;
}

function renderMobileReaderMessages() {
  if (!mobileReaderMessages) return;
  mobileReaderMessages.replaceChildren();
  mobileConversationState.messages.forEach((message) => {
    if (mobileConversationState.gapsBeforeMessageIds.has(message.message_id)) {
      mobileReaderMessages.appendChild(el(
        "p",
        "yunos-mobile-reader-gap",
        t("offline.uncachedGap", {}, "此处还有未缓存消息"),
      ));
    }
    mobileReaderMessages.appendChild(mobileReaderMessageElement(message));
  });
}

function mobileReaderRetryButton(direction) {
  const retry = el(
    "button",
    "yunos-mobile-reader-retry",
    t("common.reload", {}, "重新加载"),
  );
  retry.type = "button";
  retry.addEventListener("click", () => loadMobileConversationSegment(direction).catch(() => {}));
  return retry;
}

function mobileReaderTechnicalDetails(detail) {
  return technicalDetailsElement(detail, "yunos-mobile-reader-error-details");
}

function renderMobileReaderBoundary(target, direction) {
  if (!target) return;
  target.replaceChildren();
  const isBefore = direction === "before";
  const loading = isBefore
    ? mobileConversationState.loadingBefore
    : mobileConversationState.loadingAfter;
  const error = isBefore
    ? mobileConversationState.beforeError
    : mobileConversationState.afterError;
  const technicalDetail = isBefore
    ? mobileConversationState.beforeTechnicalDetail
    : mobileConversationState.afterTechnicalDetail;
  const hasMore = isBefore
    ? mobileConversationState.hasBefore
    : mobileConversationState.hasAfter;
  if (mobileConversationState.offline) {
    const boundary = isBefore
      ? mobileConversationState.reachedStart
      : mobileConversationState.reachedEnd;
    const message = boundary === true
      ? (isBefore
        ? t("mobile.reachedConversationStart", {}, "已到达会话开头")
        : t("mobile.reachedConversationEnd", {}, "已到达会话结尾"))
      : boundary === false
        ? (isBefore
          ? t("offline.earlierMessagesNotCached", {}, "更早消息未缓存")
          : t("offline.laterMessagesNotCached", {}, "后续消息未缓存"))
        : (isBefore
          ? t("offline.earlierStateUnknown", {}, "更早内容状态未知")
          : t("offline.laterStateUnknown", {}, "后续内容状态未知"));
    target.appendChild(el(
      "span",
      `yunos-mobile-reader-boundary ${boundary === true ? "is-end" : "is-missing"}`,
      message,
    ));
    return;
  }
  if (loading) {
    target.appendChild(el(
      "span",
      "yunos-mobile-reader-boundary is-loading",
      isBefore
        ? t("mobile.loadingEarlierMessages", {}, "正在加载更早消息…")
        : t("mobile.loadingLaterMessages", {}, "正在加载后续消息…"),
    ));
    return;
  }
  if (error) {
    const errorRow = el("div", "yunos-mobile-reader-boundary is-error");
    errorRow.append(el("span", "", error));
    const disclosure = mobileReaderTechnicalDetails(technicalDetail);
    if (disclosure) errorRow.appendChild(disclosure);
    errorRow.appendChild(mobileReaderRetryButton(direction));
    target.appendChild(errorRow);
    return;
  }
  if (!hasMore && mobileConversationState.messages.length) {
    target.appendChild(el(
      "span",
      "yunos-mobile-reader-boundary is-end",
      isBefore
        ? t("mobile.reachedConversationStart", {}, "已到达会话开头")
        : t("mobile.reachedConversationEnd", {}, "已到达会话结尾"),
    ));
  }
}

function renderMobileReaderOfflineBanner() {
  if (!mobileReaderOfflineBanner) return;
  mobileReaderOfflineBanner.hidden = !mobileConversationState.offline;
  if (mobileConversationState.offline && mobileReaderCacheTime) {
    mobileReaderCacheTime.textContent = formatMobileCacheTime(
      mobileConversationState.cachedAt,
    );
  }
}

function renderMobileReaderStatuses() {
  renderMobileReaderOfflineBanner();
  if (mobileConversationState.loadingInitial) {
    if (mobileReaderTopStatus) {
      mobileReaderTopStatus.replaceChildren(el(
        "span",
        "yunos-mobile-reader-boundary is-loading",
        t("mobile.locatingMatchedMessage", {}, "正在定位命中消息…"),
      ));
    }
    mobileReaderBottomStatus?.replaceChildren();
    return;
  }
  if (mobileConversationState.initialError) {
    if (mobileReaderTopStatus) {
      const errorRow = el("div", "yunos-mobile-reader-boundary is-error");
      errorRow.append(el("span", "", mobileConversationState.initialError));
      const disclosure = mobileReaderTechnicalDetails(
        mobileConversationState.initialTechnicalDetail,
      );
      if (disclosure) errorRow.appendChild(disclosure);
      errorRow.appendChild(mobileReaderRetryButton("initial"));
      mobileReaderTopStatus.replaceChildren(errorRow);
    }
    mobileReaderBottomStatus?.replaceChildren();
    return;
  }
  renderMobileReaderBoundary(mobileReaderTopStatus, "before");
  renderMobileReaderBoundary(mobileReaderBottomStatus, "after");
}

function captureMobileReaderSnapshot() {
  const key = mobileConversationState.activeKey;
  if (!key || !mobileConversationState.conversationId) return;
  mobileConversationState.scrollTop = mobileReaderScroll?.scrollTop || mobileConversationState.scrollTop;
  mobileConversationState.snapshots.set(key, {
    conversation: mobileConversationState.conversation,
    conversationId: mobileConversationState.conversationId,
    focusMessageId: mobileConversationState.focusMessageId,
    query: mobileConversationState.query,
    messages: mobileConversationState.messages.map((message) => ({ ...message })),
    beforeCursor: mobileConversationState.beforeCursor,
    afterCursor: mobileConversationState.afterCursor,
    hasBefore: mobileConversationState.hasBefore,
    hasAfter: mobileConversationState.hasAfter,
    scrollTop: mobileConversationState.scrollTop,
    expandedTraceIds: [...mobileConversationState.expandedTraceIds],
    offline: mobileConversationState.offline,
    cachedAt: mobileConversationState.cachedAt,
    reachedStart: mobileConversationState.reachedStart,
    reachedEnd: mobileConversationState.reachedEnd,
    gapsBeforeMessageIds: [...mobileConversationState.gapsBeforeMessageIds],
    cachedSegmentKeys: [...mobileConversationState.cachedSegmentKeys],
    topbarVisible: mobileConversationState.topbarVisibility.get(
      mobileConversationState.conversationId,
    ) !== false,
  });
}

function restoreMobileReaderSnapshot(key) {
  const snapshot = mobileConversationState.snapshots.get(key);
  if (!snapshot) return false;
  mobileConversationState.activeKey = key;
  mobileConversationState.conversation = snapshot.conversation;
  mobileConversationState.conversationId = snapshot.conversationId;
  mobileConversationState.focusMessageId = snapshot.focusMessageId;
  mobileConversationState.query = snapshot.query;
  mobileConversationState.messages = snapshot.messages.map((message) => ({ ...message }));
  mobileConversationState.messageIds = new Set(
    mobileConversationState.messages.map((message) => message.message_id),
  );
  mobileConversationState.beforeCursor = snapshot.beforeCursor;
  mobileConversationState.afterCursor = snapshot.afterCursor;
  mobileConversationState.hasBefore = snapshot.hasBefore;
  mobileConversationState.hasAfter = snapshot.hasAfter;
  mobileConversationState.scrollTop = snapshot.scrollTop;
  mobileConversationState.expandedTraceIds = new Set(snapshot.expandedTraceIds || []);
  mobileConversationState.offline = Boolean(snapshot.offline);
  mobileConversationState.cachedAt = Number(snapshot.cachedAt) || 0;
  mobileConversationState.reachedStart = snapshot.reachedStart ?? "unknown";
  mobileConversationState.reachedEnd = snapshot.reachedEnd ?? "unknown";
  mobileConversationState.gapsBeforeMessageIds = new Set(
    snapshot.gapsBeforeMessageIds || [],
  );
  mobileConversationState.cachedSegmentKeys = [...(snapshot.cachedSegmentKeys || [])];
  mobileConversationState.initialError = "";
  mobileConversationState.beforeError = "";
  mobileConversationState.afterError = "";
  mobileConversationState.initialTechnicalDetail = "";
  mobileConversationState.beforeTechnicalDetail = "";
  mobileConversationState.afterTechnicalDetail = "";
  mobileConversationState.loadingInitial = false;
  mobileConversationState.loadingBefore = false;
  mobileConversationState.loadingAfter = false;
  mobileConversationState.suppressEdgeLoading = false;
  if (mobileReaderTitle) {
    mobileReaderTitle.textContent = snapshot.conversation?.title
      || t("search.untitledConversation", {}, "未命名会话");
  }
  setMobileReaderTopbarVisible(snapshot.topbarVisible, { remember: true });
  renderMobileReaderMessages();
  renderMobileReaderStatuses();
  setMobileView("reader");
  protectActiveMobileReaderCache().catch(() => {});
  window.requestAnimationFrame(() => {
    if (mobileReaderScroll) mobileReaderScroll.scrollTop = snapshot.scrollTop || 0;
  });
  return true;
}

function firstVisibleMobileReaderMessage() {
  if (!mobileReaderScroll || !mobileReaderMessages) return null;
  const scrollRect = mobileReaderScroll.getBoundingClientRect();
  return [...mobileReaderMessages.querySelectorAll(".yunos-mobile-reader-message")]
    .find((node) => node.getBoundingClientRect().bottom > scrollRect.top + 1) || null;
}

function positionMobileConversationAnchor() {
  if (!mobileReaderScroll || !mobileConversationState.focusMessageId) return;
  mobileConversationState.suppressEdgeLoading = true;
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      const selector = `[data-message-id="${CSS.escape(mobileConversationState.focusMessageId)}"]`;
      const focus = mobileReaderMessages?.querySelector(selector);
      if (focus) {
        const scrollRect = mobileReaderScroll.getBoundingClientRect();
        const focusRect = focus.getBoundingClientRect();
        const focusTop = focusRect.top - scrollRect.top + mobileReaderScroll.scrollTop;
        mobileReaderScroll.scrollTop = Math.max(
          0,
          focusTop - Math.min(160, mobileReaderScroll.clientHeight * 0.22),
        );
      }
      mobileConversationState.scrollTop = mobileReaderScroll.scrollTop;
      mobileConversationState.positioned = true;
      captureMobileReaderSnapshot();
      window.requestAnimationFrame(() => {
        mobileConversationState.suppressEdgeLoading = false;
      });
    });
  });
}

async function protectActiveMobileReaderCache(cacheOverride = null) {
  if (
    !mobileConversationState.conversationId
    || mobileShell?.dataset.mobileView !== "reader"
  ) return;
  const cache = cacheOverride || await mobilePrivateCacheReady;
  if (!cache) return;
  await cache.markActiveReaderWindow({
    conversationId: mobileConversationState.conversationId,
    segmentKeys: mobileConversationState.cachedSegmentKeys,
    messageIds: mobileConversationState.messages.map(
      (message) => message.message_id,
    ),
  });
  if (!mobileConversationState.activeProtectionTimer) {
    mobileConversationState.activeProtectionTimer = window.setInterval(() => {
      protectActiveMobileReaderCache().catch(() => {});
    }, MOBILE_ACTIVE_CACHE_REFRESH_MS);
  }
}

async function releaseActiveMobileReaderCache() {
  if (mobileConversationState.activeProtectionTimer) {
    window.clearInterval(mobileConversationState.activeProtectionTimer);
    mobileConversationState.activeProtectionTimer = 0;
  }
  const cache = await mobilePrivateCacheReady;
  await cache?.releaseActiveReaderWindow();
}

function resetMobileConversationState(conversationId, focusMessageId, query) {
  releaseActiveMobileReaderCache().catch(() => {});
  Object.values(mobileConversationState.abortControllers).forEach((controller) => controller?.abort());
  mobileConversationState.conversation = null;
  mobileConversationState.conversationId = conversationId;
  mobileConversationState.focusMessageId = focusMessageId;
  mobileConversationState.query = query;
  mobileConversationState.messages = [];
  mobileConversationState.messageIds = new Set();
  mobileConversationState.beforeCursor = "";
  mobileConversationState.afterCursor = "";
  mobileConversationState.hasBefore = false;
  mobileConversationState.hasAfter = false;
  mobileConversationState.loadingInitial = false;
  mobileConversationState.loadingBefore = false;
  mobileConversationState.loadingAfter = false;
  mobileConversationState.initialError = "";
  mobileConversationState.beforeError = "";
  mobileConversationState.afterError = "";
  mobileConversationState.initialTechnicalDetail = "";
  mobileConversationState.beforeTechnicalDetail = "";
  mobileConversationState.afterTechnicalDetail = "";
  mobileConversationState.abortControllers = { initial: null, before: null, after: null };
  mobileConversationState.positioned = false;
  mobileConversationState.suppressEdgeLoading = true;
  mobileConversationState.scrollTop = 0;
  mobileConversationState.expandedTraceIds = new Set();
  mobileConversationState.offline = false;
  mobileConversationState.cachedAt = 0;
  mobileConversationState.reachedStart = "unknown";
  mobileConversationState.reachedEnd = "unknown";
  mobileConversationState.gapsBeforeMessageIds = new Set();
  mobileConversationState.cachedSegmentKeys = [];
  mobileConversationState.revalidating = false;
  mobileConversationState.preserveScrollTopOnReload = null;
  mobileReaderMessages?.replaceChildren();
  renderMobileReaderStatuses();
}

function applyCachedMobileConversation(cached, { preserveVisible = null } = {}) {
  mobileConversationState.conversation = {
    conversation_id: cached.conversationId,
    title: cached.conversationTitle
      || t("search.untitledConversation", {}, "未命名会话"),
  };
  mobileConversationState.messages = cached.messages.map((message) => (
    cachedMessageForMobile(message, mobileConversationState.focusMessageId)
  ));
  mobileConversationState.messageIds = new Set(
    mobileConversationState.messages.map((message) => message.message_id),
  );
  mobileConversationState.beforeCursor = "";
  mobileConversationState.afterCursor = "";
  mobileConversationState.hasBefore = false;
  mobileConversationState.hasAfter = false;
  mobileConversationState.offline = true;
  mobileConversationState.cachedAt = Number(cached.cachedAt) || 0;
  mobileConversationState.reachedStart = cached.reachedStart ?? "unknown";
  mobileConversationState.reachedEnd = cached.reachedEnd ?? "unknown";
  mobileConversationState.gapsBeforeMessageIds = new Set(
    cached.gapsBeforeMessageIds || [],
  );
  mobileConversationState.cachedSegmentKeys = [...(cached.segmentKeys || [])];
  mobileConversationState.loadingInitial = false;
  mobileConversationState.loadingBefore = false;
  mobileConversationState.loadingAfter = false;
  mobileConversationState.initialError = "";
  mobileConversationState.beforeError = "";
  mobileConversationState.afterError = "";
  mobileConversationState.initialTechnicalDetail = "";
  mobileConversationState.beforeTechnicalDetail = "";
  mobileConversationState.afterTechnicalDetail = "";
  if (mobileReaderTitle) {
    mobileReaderTitle.textContent = cached.conversationTitle
      || t("search.untitledConversation", {}, "未命名会话");
  }
  renderMobileReaderMessages();
  renderMobileReaderStatuses();
  protectActiveMobileReaderCache().catch(() => {});
  if (preserveVisible?.messageId) {
    window.requestAnimationFrame(() => {
      const selector = `[data-message-id="${CSS.escape(preserveVisible.messageId)}"]`;
      const node = mobileReaderMessages?.querySelector(selector);
      if (!node || !mobileReaderScroll) return;
      const scrollRect = mobileReaderScroll.getBoundingClientRect();
      const nodeOffset = node.getBoundingClientRect().top - scrollRect.top;
      mobileReaderScroll.scrollTop += nodeOffset - preserveVisible.offset;
    });
  } else {
    positionMobileConversationAnchor();
  }
}

async function tryCachedMobileConversationFallback({
  error,
  kind,
  requestCursor,
  preserveVisible = null,
}) {
  if (!allowsPrivateCacheFallback(error)) return { used: false, status: "" };
  const cache = await mobilePrivateCacheReady;
  if (!cache) return { used: false, status: "missing" };
  const cached = await cache.getCachedConversationWindow({
    conversationId: mobileConversationState.conversationId,
    anchorMessageId: mobileConversationState.focusMessageId,
    direction: kind === "initial" ? "" : kind,
    requestCursor: kind === "initial" ? "" : requestCursor,
  }).catch(() => ({ status: "incomplete" }));
  if (cached.status !== "ready") return { used: false, status: cached.status };
  applyCachedMobileConversation(cached, { preserveVisible });
  return { used: true, status: "ready" };
}

async function loadMobileConversationSegment(direction = "initial") {
  if (!mobileConversationState.conversationId) return;
  const isBefore = direction === "before";
  const isAfter = direction === "after";
  const kind = isBefore ? "before" : (isAfter ? "after" : "initial");
  const requestCursor = isBefore
    ? mobileConversationState.beforeCursor
    : (isAfter ? mobileConversationState.afterCursor : "");
  if (kind === "before" && (
    mobileConversationState.loadingBefore
    || !mobileConversationState.hasBefore
    || !mobileConversationState.beforeCursor
  )) return;
  if (kind === "after" && (
    mobileConversationState.loadingAfter
    || !mobileConversationState.hasAfter
    || !mobileConversationState.afterCursor
  )) return;
  if (kind === "initial" && mobileConversationState.loadingInitial) return;

  mobileConversationState.abortControllers[kind]?.abort();
  const controller = new AbortController();
  mobileConversationState.abortControllers[kind] = controller;
  const serial = ++mobileConversationState.requestSerial[kind];
  if (kind === "initial") {
    mobileConversationState.loadingInitial = true;
    mobileConversationState.initialError = "";
    mobileConversationState.initialTechnicalDetail = "";
  } else if (kind === "before") {
    mobileConversationState.loadingBefore = true;
    mobileConversationState.beforeError = "";
    mobileConversationState.beforeTechnicalDetail = "";
  } else {
    mobileConversationState.loadingAfter = true;
    mobileConversationState.afterError = "";
    mobileConversationState.afterTechnicalDetail = "";
  }
  renderMobileReaderStatuses();

  const visibleBeforeRequest = kind === "initial" ? null : firstVisibleMobileReaderMessage();
  const preserveVisible = visibleBeforeRequest && mobileReaderScroll
    ? {
      messageId: visibleBeforeRequest.dataset.messageId || "",
      offset: visibleBeforeRequest.getBoundingClientRect().top
        - mobileReaderScroll.getBoundingClientRect().top,
    }
    : null;
  try {
    const data = await messageApi(
      mobileConversationUrl(kind),
      { signal: controller.signal },
    );
    if (serial !== mobileConversationState.requestSerial[kind]) return;
    const incoming = Array.isArray(data.messages) ? data.messages : [];
    const cursors = data.cursors || {};
    mobileConversationState.offline = false;
    mobileConversationState.cachedAt = 0;
    mobileConversationState.reachedStart = typeof data.has_before === "boolean"
      ? !data.has_before
      : "unknown";
    mobileConversationState.reachedEnd = typeof data.has_after === "boolean"
      ? !data.has_after
      : "unknown";
    mobileConversationState.gapsBeforeMessageIds = new Set();
    if (kind === "initial") {
      mobileSearchState.sourceMode = "online";
      mobileConversationState.conversation = data.conversation || {};
      mobileConversationState.focusMessageId = data.focus?.message_id
        || mobileConversationState.focusMessageId;
      mobileConversationState.messages = incoming;
      mobileConversationState.messageIds = new Set(incoming.map((message) => message.message_id));
      mobileConversationState.beforeCursor = cursors.before || "";
      mobileConversationState.afterCursor = cursors.after || "";
      mobileConversationState.hasBefore = Boolean(data.has_before);
      mobileConversationState.hasAfter = Boolean(data.has_after);
      mobileConversationState.loadingInitial = false;
      if (mobileReaderTitle) {
        mobileReaderTitle.textContent = data.conversation?.title
          || t("search.untitledConversation", {}, "未命名会话");
      }
      renderMobileReaderMessages();
      renderMobileReaderStatuses();
      recordMobileRecentConversation(data.conversation || {});
      const preserved = mobileConversationState.preserveScrollTopOnReload;
      mobileConversationState.preserveScrollTopOnReload = null;
      mobileConversationState.revalidating = false;
      if (preserved?.messageId) {
        window.requestAnimationFrame(() => {
          window.requestAnimationFrame(() => {
            const selector = `[data-message-id="${CSS.escape(preserved.messageId)}"]`;
            const node = mobileReaderMessages?.querySelector(selector);
            if (!node || !mobileReaderScroll) {
              positionMobileConversationAnchor();
              return;
            }
            const restoreOffset = () => {
              const scrollRect = mobileReaderScroll.getBoundingClientRect();
              const nodeOffset = node.getBoundingClientRect().top - scrollRect.top;
              mobileReaderScroll.scrollTop += nodeOffset - preserved.offset;
            };
            restoreOffset();
            window.requestAnimationFrame(() => {
              restoreOffset();
              captureMobileReaderSnapshot();
            });
          });
        });
      } else {
        positionMobileConversationAnchor();
      }
      saveMobileMessageResponse({
        data,
        direction: "initial",
      }).catch(reportMobileCacheWriteFailure);
      scheduleMobileRestoreStateSave({ interaction: false });
      return;
    }

    const newMessages = incoming.filter(
      (message) => !mobileConversationState.messageIds.has(message.message_id),
    );
    if (!newMessages.length && incoming.length) {
      throw new Error(t(
        "error.duplicateMessagePage",
        {},
        "服务器返回了重复的消息分页，请重新加载。",
      ));
    }
    const visibleAnchor = kind === "before" ? firstVisibleMobileReaderMessage() : null;
    const anchorTop = visibleAnchor?.getBoundingClientRect().top ?? 0;
    const fragment = document.createDocumentFragment();
    newMessages.forEach((message) => {
      mobileConversationState.messageIds.add(message.message_id);
      fragment.appendChild(mobileReaderMessageElement(message));
    });
    if (kind === "before") {
      mobileReaderMessages?.prepend(fragment);
      mobileConversationState.messages = [...newMessages, ...mobileConversationState.messages];
      mobileConversationState.beforeCursor = cursors.before || "";
      mobileConversationState.hasBefore = Boolean(data.has_before);
      mobileConversationState.loadingBefore = false;
      if (visibleAnchor && mobileReaderScroll) {
        const afterTop = visibleAnchor.getBoundingClientRect().top;
        mobileReaderScroll.scrollTop += afterTop - anchorTop;
      }
    } else {
      mobileReaderMessages?.appendChild(fragment);
      mobileConversationState.messages = [...mobileConversationState.messages, ...newMessages];
      mobileConversationState.afterCursor = cursors.after || "";
      mobileConversationState.hasAfter = Boolean(data.has_after);
      mobileConversationState.loadingAfter = false;
    }
    renderMobileReaderStatuses();
    mobileConversationState.scrollTop = mobileReaderScroll?.scrollTop || 0;
    captureMobileReaderSnapshot();
    saveMobileMessageResponse({
      data,
      direction: kind,
      requestCursor,
    }).catch(reportMobileCacheWriteFailure);
    scheduleMobileRestoreStateSave({ interaction: false });
  } catch (error) {
    if (error?.name === "AbortError" || serial !== mobileConversationState.requestSerial[kind]) return;
    const fallback = await tryCachedMobileConversationFallback({
      error,
      kind,
      requestCursor,
      preserveVisible,
    });
    if (serial !== mobileConversationState.requestSerial[kind]) return;
    if (fallback.used) return;
    const technicalDetail = error?.technicalDetail
      || error?.message
      || t("error.requestFailed", {}, "请求失败");
    const offlineMessage = allowsPrivateCacheFallback(error)
      ? cacheFallbackMessage(
        { status: fallback.status },
        t("mobile.conversation", {}, "会话"),
      )
      : "";
    if (kind === "initial") {
      mobileConversationState.loadingInitial = false;
      mobileConversationState.initialError = offlineMessage
        || t("error.conversationLoadFailedSummary", {}, "会话加载失败，请重试。");
      mobileConversationState.initialTechnicalDetail = technicalDetail;
    } else if (kind === "before") {
      mobileConversationState.loadingBefore = false;
      mobileConversationState.beforeError = offlineMessage
        || t("error.earlierMessagesLoadFailedSummary", {}, "无法加载更早的消息，请重试。");
      mobileConversationState.beforeTechnicalDetail = technicalDetail;
    } else {
      mobileConversationState.loadingAfter = false;
      mobileConversationState.afterError = offlineMessage
        || t("error.laterMessagesLoadFailedSummary", {}, "无法加载更新的消息，请重试。");
      mobileConversationState.afterTechnicalDetail = technicalDetail;
    }
    renderMobileReaderStatuses();
  } finally {
    if (serial === mobileConversationState.requestSerial[kind]) {
      mobileConversationState.abortControllers[kind] = null;
    }
  }
}

function mobileReaderUrl(conversationId, focusMessageId) {
  const url = new URL(window.location.href);
  url.searchParams.set("mobile_conversation", conversationId);
  url.searchParams.set("focus_message", focusMessageId);
  return `${url.pathname}${url.search}${url.hash}`;
}

function mobileResultsUrl() {
  const url = new URL(window.location.href);
  url.searchParams.delete("mobile_conversation");
  url.searchParams.delete("focus_message");
  return `${url.pathname}${url.search}${url.hash}`;
}

function openMobileConversationReader(group, hit) {
  const conversationId = String(hit?.conversation_id || group?.conversation_id || "").trim();
  const focusMessageId = String(hit?.message_id || "").trim();
  if (!conversationId || !focusMessageId) return;
  captureMobileReaderSnapshot();
  mobileSearchState.resultsScrollTop = mobileResultsPage?.scrollTop || 0;
  const key = mobileConversationKey(conversationId, focusMessageId);
  const resultsState = {
    ...(window.history.state || {}),
    yunosMobileView: "results",
    resultsScrollTop: mobileSearchState.resultsScrollTop,
  };
  window.history.replaceState(resultsState, "", mobileResultsUrl());
  window.history.pushState(
    { yunosMobileView: "reader", readerKey: key },
    "",
    mobileReaderUrl(conversationId, focusMessageId),
  );

  if (restoreMobileReaderSnapshot(key)) return;
  mobileConversationState.activeKey = key;
  resetMobileConversationState(
    conversationId,
    focusMessageId,
    mobileSearchState.submittedQuery || mobileSearchState.query,
  );
  mobileConversationState.activeKey = key;
  if (mobileReaderTitle) {
    mobileReaderTitle.textContent = group?.title
      || t("search.untitledConversation", {}, "未命名会话");
  }
  const topbarVisible = mobileConversationState.topbarVisibility.has(conversationId)
    ? mobileConversationState.topbarVisibility.get(conversationId)
    : true;
  setMobileReaderTopbarVisible(topbarVisible, { remember: true });
  setMobileView("reader");
  scheduleMobileRestoreStateSave({ interaction: true });
  if (hit?.source_mode === "cached-only") {
    mobilePrivateCacheReady.then(async (cache) => {
      const cached = await cache?.getCachedConversationWindow({
        conversationId,
        anchorMessageId: focusMessageId,
      });
      if (cached?.status === "ready") {
        applyCachedMobileConversation(cached);
        scheduleMobileRestoreStateSave({ interaction: false });
        return;
      }
      mobileConversationState.initialError = cacheFallbackMessage(
        cached || { status: "missing" },
        t("mobile.conversation", {}, "会话"),
      );
      renderMobileReaderStatuses();
    }).catch(() => {
      mobileConversationState.initialError = t(
        "error.conversationCacheUnavailable",
        {},
        "会话缓存暂不可用",
      );
      renderMobileReaderStatuses();
    });
    return;
  }
  loadMobileConversationSegment("initial").catch(() => {});
}

function showMobileSearchResultsFromReader(state = {}) {
  captureMobileReaderSnapshot();
  releaseActiveMobileReaderCache().catch(() => {});
  setMobileView("results");
  const savedScrollTop = Number.isFinite(Number(state.resultsScrollTop))
    ? Number(state.resultsScrollTop)
    : mobileSearchState.resultsScrollTop;
  window.requestAnimationFrame(() => {
    if (mobileResultsPage) mobileResultsPage.scrollTop = savedScrollTop || 0;
  });
  scheduleMobileRestoreStateSave({ interaction: true });
}

function reloadMobileConversationFromNetwork() {
  if (!mobileConversationState.conversationId || mobileConversationState.loadingInitial) return;
  const visible = firstVisibleMobileReaderMessage();
  if (visible && mobileReaderScroll) {
    const messageId = visible.dataset.messageId || mobileConversationState.focusMessageId;
    mobileConversationState.focusMessageId = messageId;
    mobileConversationState.preserveScrollTopOnReload = {
      messageId,
      offset: visible.getBoundingClientRect().top
        - mobileReaderScroll.getBoundingClientRect().top,
    };
  }
  mobileConversationState.revalidating = true;
  mobileConversationState.initialError = "";
  loadMobileConversationSegment("initial").catch(() => {});
}

function returnFromMobileConversationReader() {
  if (window.history.state?.yunosMobileView === "reader") {
    window.history.back();
    return;
  }
  showMobileSearchResultsFromReader({ resultsScrollTop: mobileSearchState.resultsScrollTop });
}

function handleMobileReaderPopState(event) {
  const entry = event.state || {};
  if (entry.yunosMobileView === "reader" && entry.readerKey) {
    if (restoreMobileReaderSnapshot(entry.readerKey)) return;
  }
  if (mobileShell?.dataset.mobileView === "reader") {
    showMobileSearchResultsFromReader(entry);
  }
}

function handleMobileReaderScroll() {
  if (!mobileReaderScroll) return;
  mobileConversationState.scrollTop = mobileReaderScroll.scrollTop;
  window.clearTimeout(mobileStatePersistence.scrollTimer);
  mobileStatePersistence.scrollTimer = window.setTimeout(() => {
    mobileStatePersistence.scrollTimer = 0;
    scheduleMobileRestoreStateSave({
      interaction: true,
      delay: 0,
    });
  }, MOBILE_SCROLL_SAVE_DEBOUNCE_MS);
  if (mobileConversationState.suppressEdgeLoading || mobileConversationState.loadingInitial) return;
  if (mobileReaderScroll.scrollTop < 260) {
    loadMobileConversationSegment("before").catch(() => {});
  }
  const remaining = mobileReaderScroll.scrollHeight
    - mobileReaderScroll.clientHeight
    - mobileReaderScroll.scrollTop;
  if (remaining < 320) loadMobileConversationSegment("after").catch(() => {});
}

function readMobileHistory() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(MOBILE_SEARCH_HISTORY_KEY) || "{}");
    return {
      searches: Array.isArray(parsed.searches) ? parsed.searches.slice(0, MOBILE_SEARCH_HISTORY_LIMIT) : [],
      conversations: Array.isArray(parsed.conversations)
        ? parsed.conversations.slice(0, MOBILE_SEARCH_HISTORY_LIMIT)
        : [],
    };
  } catch {
    return { searches: [], conversations: [] };
  }
}

function writeMobileHistory(history) {
  try {
    window.localStorage.setItem(MOBILE_SEARCH_HISTORY_KEY, JSON.stringify({
      searches: (history.searches || []).slice(0, MOBILE_SEARCH_HISTORY_LIMIT),
      conversations: (history.conversations || []).slice(0, MOBILE_SEARCH_HISTORY_LIMIT),
    }));
  } catch {
    // Local history is optional and must never block read-only search.
  }
}

function recordMobileRecentSearch(query) {
  const cleanQuery = String(query || "").trim();
  if (!cleanQuery) return;
  const history = readMobileHistory();
  history.searches = [
    { query: cleanQuery, visited_at: new Date().toISOString() },
    ...history.searches.filter((item) => item?.query !== cleanQuery),
  ].slice(0, MOBILE_SEARCH_HISTORY_LIMIT);
  writeMobileHistory(history);
  if (mobileRecentDrawer?.dataset.open === "true") renderMobileRecentDrawer();
}

function recordMobileRecentConversation(conversation = {}) {
  const conversationId = String(conversation.conversation_id || "").trim();
  if (!conversationId) return;
  const history = readMobileHistory();
  history.conversations = [
    {
      conversation_id: conversationId,
      title: conversation.title
        || conversation.conversation_title
        || t("search.untitledConversation", {}, "未命名会话"),
      update_time_iso: conversation.update_time_iso || "",
      visited_at: new Date().toISOString(),
    },
    ...history.conversations.filter((item) => item?.conversation_id !== conversationId),
  ].slice(0, MOBILE_SEARCH_HISTORY_LIMIT);
  writeMobileHistory(history);
  if (mobileRecentDrawer?.dataset.open === "true") renderMobileRecentDrawer();
}

function formatMobileHistoryTime(value) {
  const date = new Date(value || "");
  if (Number.isNaN(date.getTime())) return t("date.justNow", {}, "刚刚");
  return formatUiRelativeTime(date);
}

function deleteMobileHistoryItem(kind, key) {
  const history = readMobileHistory();
  if (kind === "searches") {
    history.searches = history.searches.filter((item) => item?.query !== key);
  } else {
    history.conversations = history.conversations.filter((item) => item?.conversation_id !== key);
  }
  writeMobileHistory(history);
  renderMobileRecentDrawer();
}

function mobileHistoryDeleteButton(label, onDelete) {
  const buttonElement = el(
    "button",
    "yunos-mobile-history-delete",
    t("common.delete", {}, "删除"),
  );
  buttonElement.type = "button";
  buttonElement.setAttribute("aria-label", label);
  buttonElement.addEventListener("click", onDelete);
  return buttonElement;
}

function mobileRecentSearchRow(item) {
  const row = el("div", "yunos-mobile-history-row");
  const open = el("button", "yunos-mobile-history-open");
  open.type = "button";
  open.append(
    el("span", "yunos-mobile-history-title", item.query || ""),
    el("span", "yunos-mobile-history-time", formatMobileHistoryTime(item.visited_at)),
  );
  open.addEventListener("click", () => {
    closeMobileRecentDrawer();
    if (!mobileSearchInput) return;
    mobileSearchInput.value = item.query || "";
    mobileSearchState.query = mobileSearchInput.value.trim();
    mobileSearchClear?.toggleAttribute("hidden", !mobileSearchInput.value);
    setMobileSearchHint("");
    if (
      mobileSearchState.completedFirstPageKey === mobileFirstPageKey(mobileSearchState.query)
      && mobileSearchState.submittedQuery === mobileSearchState.query
    ) {
      setMobileView("results");
      renderMobileSearchResults();
      return;
    }
    runMobileSearch({ reset: true }).catch(() => {});
  });
  row.append(
    open,
    mobileHistoryDeleteButton(t(
      "aria.deleteRecentSearch",
      { query: item.query || "" },
      `删除最近搜索：${item.query || ""}`,
    ), () => {
      deleteMobileHistoryItem("searches", item.query || "");
    }),
  );
  return row;
}

function mobileRecentConversationRow(item) {
  const row = el("div", "yunos-mobile-history-row");
  const copy = el("div", "yunos-mobile-history-copy");
  copy.append(
    el(
      "span",
      "yunos-mobile-history-title",
      item.title || t("search.untitledConversation", {}, "未命名会话"),
    ),
    el(
      "span",
      "yunos-mobile-history-time",
      t(
        "mobile.recentlyOpenedAt",
        { time: formatMobileHistoryTime(item.visited_at) },
        `最近打开 · ${formatMobileHistoryTime(item.visited_at)}`,
      ),
    ),
  );
  row.append(
    copy,
    mobileHistoryDeleteButton(t(
      "aria.deleteRecentOpened",
      { title: item.title || t("search.untitledConversation", {}, "未命名会话") },
      `删除最近打开：${item.title || "未命名会话"}`,
    ), () => {
      deleteMobileHistoryItem("conversations", item.conversation_id || "");
    }),
  );
  return row;
}

function renderMobileHistoryList(target, items, rowFactory, emptyMessage) {
  if (!target) return;
  const heading = target.querySelector(".yunos-mobile-recent-heading");
  target.replaceChildren(...(heading ? [heading] : []));
  if (!items.length) {
    target.appendChild(el("p", "yunos-mobile-history-empty", emptyMessage));
    return;
  }
  items.forEach((item) => target.appendChild(rowFactory(item)));
}

function renderMobileRecentDrawer() {
  const history = readMobileHistory();
  renderMobileHistoryList(
    mobileRecentSearches,
    history.searches,
    mobileRecentSearchRow,
    t("mobile.noRecentSearches", {}, "还没有最近搜索。"),
  );
  renderMobileHistoryList(
    mobileRecentConversations,
    history.conversations,
    mobileRecentConversationRow,
    t("mobile.noRecentlyOpened", {}, "还没有最近打开的会话。"),
  );
  if (mobileRecentStatus) {
    const hostAvailable = document.body.dataset.yunosHostAvailable !== "false" && navigator.onLine;
    mobileRecentStatus.textContent = hostAvailable
      ? t("common.online", {}, "在线")
      : t("error.hostUnavailable", {}, "主机不可用");
    mobileRecentStatus.classList.toggle("is-offline", !hostAvailable);
  }
  if (mobileRecentClear) {
    mobileRecentClear.disabled = !history.searches.length && !history.conversations.length;
  }
}

function openMobileRecentDrawer() {
  if (!mobileRecentDrawer || !mobileRecentBackdrop) return;
  if (mobileSearchState.drawerCloseTimer) {
    window.clearTimeout(mobileSearchState.drawerCloseTimer);
    mobileSearchState.drawerCloseTimer = 0;
  }
  mobileSearchState.lastFocusedElement = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : mobileRecentButton;
  renderMobileRecentDrawer();
  mobileRecentDrawer.hidden = false;
  mobileRecentBackdrop.hidden = false;
  mobileRecentDrawer.style.setProperty("--yunos-mobile-drawer-drag-y", "0px");
  window.requestAnimationFrame(() => {
    mobileRecentDrawer.dataset.open = "true";
    mobileRecentBackdrop.dataset.open = "true";
    mobileRecentDrawer.focus({ preventScroll: true });
  });
}

function closeMobileRecentDrawer({ returnFocus = true } = {}) {
  if (!mobileRecentDrawer || !mobileRecentBackdrop || mobileRecentDrawer.hidden) return;
  mobileRecentDrawer.dataset.open = "false";
  mobileRecentBackdrop.dataset.open = "false";
  mobileRecentDrawer.style.setProperty("--yunos-mobile-drawer-drag-y", "0px");
  if (mobileSearchState.drawerCloseTimer) window.clearTimeout(mobileSearchState.drawerCloseTimer);
  mobileSearchState.drawerCloseTimer = window.setTimeout(() => {
    mobileRecentDrawer.hidden = true;
    mobileRecentBackdrop.hidden = true;
    mobileSearchState.drawerCloseTimer = 0;
  }, 230);
  if (returnFocus && mobileSearchState.lastFocusedElement instanceof HTMLElement) {
    mobileSearchState.lastFocusedElement.focus({ preventScroll: true });
  }
}

function mobileOfflineConversationRow(item) {
  const button = el("button", "yunos-mobile-offline-conversation");
  button.type = "button";
  button.setAttribute("role", "listitem");
  button.append(
    el(
      "strong",
      "",
      item.conversationTitle || t("search.untitledConversation", {}, "未命名会话"),
    ),
    el(
      "span",
      "",
      t(
        "offline.cachedConversationMeta",
        {
          time: formatMobileCacheTime(item.cachedAt),
          count: formatUiNumber(item.messageCount),
        },
        `${formatMobileCacheTime(item.cachedAt)} · 约 ${formatUiNumber(item.messageCount)} 条消息`,
      ),
    ),
  );
  button.addEventListener("click", async () => {
    const conversationId = item.conversationId;
    const focusMessageId = item.anchorMessageId;
    const key = mobileConversationKey(conversationId, focusMessageId);
    window.history.pushState(
      { yunosMobileView: "reader", readerKey: key },
      "",
      mobileReaderUrl(conversationId, focusMessageId),
    );
    resetMobileConversationState(conversationId, focusMessageId, "");
    mobileConversationState.activeKey = key;
    setMobileView("reader");
    scheduleMobileRestoreStateSave({ interaction: true });
    const cache = await mobilePrivateCacheReady;
    const cached = await cache?.getCachedConversationWindow({
      conversationId,
      anchorMessageId: focusMessageId,
    });
    if (cached?.status === "ready") {
      applyCachedMobileConversation(cached);
      return;
    }
    mobileConversationState.initialError = cacheFallbackMessage(
      cached || { status: "missing" },
      t("mobile.conversation", {}, "会话"),
    );
    renderMobileReaderStatuses();
  });
  return button;
}

async function renderMobileOfflineCatalog() {
  if (!mobileOfflineConversations || !mobileOfflineStatus) return;
  const serial = ++mobileOfflineCatalogRequestSerial;
  mobileOfflineStatus.textContent = t(
    "offline.readingLocalCache",
    {},
    "正在读取本机缓存…",
  );
  mobileOfflineConversations.replaceChildren();
  const cache = await mobilePrivateCacheReady;
  if (serial !== mobileOfflineCatalogRequestSerial) return;
  if (!cache) {
    mobileOfflineStatus.textContent = t(
      "error.privateCacheUnavailable",
      {},
      "本机私人缓存不可用",
    );
    return;
  }
  try {
    const catalog = await cache.getCachedConversationCatalog();
    if (serial !== mobileOfflineCatalogRequestSerial) return;
    if (!catalog.length) {
      mobileOfflineStatus.textContent = t(
        "offline.noValidSegments",
        {},
        "当前没有仍在 30 天有效期内的完整缓存分段",
      );
      return;
    }
    const count = formatUiNumber(catalog.length);
    mobileOfflineStatus.textContent = t(
      "offline.cachedConversationCount",
      { count },
      `${count} 个已缓存会话`,
    );
    mobileOfflineConversations.replaceChildren(
      ...catalog.map((item) => mobileOfflineConversationRow(item)),
    );
  } catch (error) {
    if (serial !== mobileOfflineCatalogRequestSerial) return;
    console.warn("YunOS cached conversation catalog is unavailable.", error);
    mobileOfflineStatus.textContent = t(
      "error.cachedCatalogUnavailable",
      {},
      "已缓存会话列表暂不可用",
    );
  }
}

function syncMobileOfflineSurface(available = null) {
  if (!isMobileSearchSurfaceActive()) return;
  const hostAvailable = available === null
    ? document.body.dataset.yunosHostAvailable !== "false" && navigator.onLine
    : available !== false;
  if (mobileOfflineHome) {
    mobileOfflineHome.hidden = hostAvailable || mobileShell?.dataset.mobileView !== "home";
  }
  if (mobileSearchInput) {
    mobileSearchInput.placeholder = hostAvailable
      ? t("search.memory", {}, "搜索记忆")
      : t("offline.searchCached", {}, "搜索已缓存内容");
  }
  if (!hostAvailable && mobileSearchState.sourceMode !== "cached-only") {
    mobileSearchState.sourceMode = "cached-only";
  }
  renderMobileOfflineSearchBanner();
  if (!hostAvailable) renderMobileOfflineCatalog().catch(() => {});
}

function restoreMobileConversationRoute() {
  if (!isMobileSearchSurfaceActive()) return;
  const params = new URLSearchParams(window.location.search);
  const conversationId = String(params.get("mobile_conversation") || "").trim();
  const focusMessageId = String(params.get("focus_message") || "").trim();
  if (!conversationId || !focusMessageId) return;
  const key = mobileConversationKey(conversationId, focusMessageId);
  resetMobileConversationState(conversationId, focusMessageId, "");
  mobileConversationState.activeKey = key;
  window.history.replaceState(
    { ...(window.history.state || {}), yunosMobileView: "reader", readerKey: key },
    "",
    window.location.href,
  );
  setMobileView("reader");
  loadMobileConversationSegment("initial").catch(() => {});
}

function restoreMobileResultPosition(state) {
  const expandedConversationId = String(state.expandedConversationId || "");
  if (
    expandedConversationId
    && mobileSearchState.groups.some(
      (group) => group.conversation_id === expandedConversationId,
    )
  ) {
    mobileSearchState.expandedConversationId = expandedConversationId;
    setExpandedMobileConversation(expandedConversationId, { persist: false });
  }
  const previewRef = state.expandedPreviewRef;
  if (previewRef?.conversationId && previewRef?.messageId) {
    const group = mobileSearchState.groups.find(
      (item) => item.conversation_id === previewRef.conversationId,
    );
    const hit = group
      ? [group.best_hit, ...(group.other_hits || [])].find(
        (item) => item.message_id === previewRef.messageId,
      )
      : null;
    if (group && hit) {
      const key = mobileHitKey(hit);
      mobileSearchState.expandedPreviewKey = key;
      syncMobileExpandedPreview();
      loadMobileContextPreview(group, hit, key).catch(() => {});
    }
  }
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      if (!mobileResultsPage) return;
      const anchor = state.resultAnchorRef;
      let restored = false;
      if (anchor?.conversationId && anchor?.messageId) {
        const key = JSON.stringify([anchor.conversationId, anchor.messageId]);
        const unit = [...mobileSearchResults.querySelectorAll(".yunos-mobile-result-unit")]
          .find((candidate) => candidate.dataset.mobileHitKey === key);
        if (unit) {
          const bounds = mobileResultsPage.getBoundingClientRect();
          const offset = unit.getBoundingClientRect().top - bounds.top;
          mobileResultsPage.scrollTop += offset - (Number(state.resultAnchorOffset) || 0);
          restored = true;
        }
      }
      if (!restored) {
        mobileResultsPage.scrollTop = Math.max(0, Number(state.resultScrollTop) || 0);
      }
      mobileSearchState.resultsScrollTop = mobileResultsPage.scrollTop;
    });
  });
}

function restoreMobileReaderPosition(state) {
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      if (!mobileReaderScroll) return;
      const messageId = String(
        state.readerScrollAnchorMessageId || state.currentAnchorMessageId || "",
      );
      const selector = messageId
        ? `[data-message-id="${CSS.escape(messageId)}"]`
        : "";
      const node = selector ? mobileReaderMessages?.querySelector(selector) : null;
      if (node) {
        const bounds = mobileReaderScroll.getBoundingClientRect();
        const offset = node.getBoundingClientRect().top - bounds.top;
        mobileReaderScroll.scrollTop += offset - (Number(state.readerScrollOffset) || 0);
      } else {
        positionMobileConversationAnchor();
      }
      mobileConversationState.scrollTop = mobileReaderScroll.scrollTop;
      captureMobileReaderSnapshot();
    });
  });
}

async function returnMobileHomeAfterRestoreFailure(reason = "invalid") {
  const cache = await mobilePrivateCacheReady;
  await cache?.discardResumeState(reason).catch(() => {});
  mobileStatePersistence.lastFailureReason = reason;
  mobileStatePersistence.restoredSavedAt = 0;
  mobileStatePersistence.restoreInProgress = false;
  mobileSearchState.restoring = false;
  mobileSearchInput.value = "";
  mobileSearchState.query = "";
  mobileSearchState.submittedQuery = "";
  mobileSearchState.sourceMode = isMobileHostAvailable() ? "online" : "cached-only";
  mobileConversationState.loadingInitial = false;
  resetMobileSearchResults();
  setMobileView("home");
  window.history.replaceState(
    { ...(window.history.state || {}), yunosMobileView: "home" },
    "",
    mobileResultsUrl(),
  );
  showMobileCacheToast(
    reason === "expired"
      ? t(
        "mobile.restoreExpired",
        {},
        "上次移动状态已超过 24 小时，已回到首页",
      )
      : t(
        "mobile.restoreUnsafe",
        {},
        "上次移动状态无法安全恢复，已回到首页",
      ),
  );
  syncMobileOfflineSurface();
}

async function restoreMobileResultsState(state) {
  mobileSearchInput.value = state.query || "";
  mobileSearchState.query = mobileSearchInput.value.trim();
  mobileSearchState.submittedQuery = mobileSearchState.query;
  mobileSearchState.sort = state.sortMode === "latest" ? "latest" : "relevance";
  mobileSearchState.sourceMode = (
    state.sourceMode === "cached-only" || !isMobileHostAvailable()
  ) ? "cached-only" : "online";
  mobileSearchState.restoring = true;
  mobileSearchState.restoreTarget = state;
  mobileSearchClear?.toggleAttribute("hidden", !mobileSearchInput.value);
  if (mobileSortToggle) {
    mobileSortToggle.textContent = mobileSearchState.sort === "latest"
      ? t("search.sortLatest", {}, "最新优先")
      : t("search.sortRelevance", {}, "相关度优先");
  }
  setMobileView("results");
  const targetCount = new Set(
    (state.loadedResultRefs || []).map((ref) => ref.conversationId),
  ).size;
  const forceCached = mobileSearchState.sourceMode === "cached-only";
  await runMobileSearch({
    reset: true,
    force: true,
    forceCached,
    forceOnline: !forceCached,
    restoreLimit: Math.max(MOBILE_SEARCH_PAGE_SIZE, targetCount),
  });
  while (
    mobileSearchState.hasMore
    && mobileSearchState.groups.length < targetCount
    && mobileSearchState.groups.length < 500
  ) {
    await runMobileSearch({
      reset: false,
      force: true,
      forceCached: mobileSearchState.sourceMode === "cached-only",
      forceOnline: mobileSearchState.sourceMode === "online",
    });
  }
  restoreMobileResultPosition(state);
  mobileSearchState.restoring = false;
  mobileSearchState.restoreTarget = null;
  scheduleMobileRestoreStateSave({ interaction: false, delay: 0 });
}

async function restoreMobileReaderState(state) {
  const conversationId = String(state.currentConversationId || "");
  const focusMessageId = String(state.currentAnchorMessageId || "");
  if (mobileSearchInput) mobileSearchInput.value = state.query || "";
  mobileSearchState.query = String(state.query || "").trim();
  mobileSearchState.submittedQuery = mobileSearchState.query;
  mobileSearchState.sort = state.sortMode === "latest" ? "latest" : "relevance";
  mobileSearchState.sourceMode = (
    state.sourceMode === "cached-only" || !isMobileHostAvailable()
  ) ? "cached-only" : "online";
  mobileSearchClear?.toggleAttribute("hidden", !mobileSearchState.query);
  if (mobileSortToggle) {
    mobileSortToggle.textContent = mobileSearchState.sort === "latest"
      ? t("search.sortLatest", {}, "最新优先")
      : t("search.sortRelevance", {}, "相关度优先");
  }
  const key = mobileConversationKey(conversationId, focusMessageId);
  resetMobileConversationState(conversationId, focusMessageId, state.query || "");
  mobileConversationState.loadingInitial = true;
  mobileConversationState.activeKey = key;
  setMobileReaderTopbarVisible(!state.topBarHidden, { remember: true });
  setMobileView("reader");
  window.history.replaceState(
    { ...(window.history.state || {}), yunosMobileView: "reader", readerKey: key },
    "",
    mobileReaderUrl(conversationId, focusMessageId),
  );
  const useCache = state.sourceMode === "cached-only" || !isMobileHostAvailable();
  if (useCache) {
    const cache = await mobilePrivateCacheReady;
    const cached = await cache?.getCachedConversationWindow({
      conversationId,
      anchorMessageId: focusMessageId,
    });
    if (cached?.status !== "ready") {
      await returnMobileHomeAfterRestoreFailure(`reader_${cached?.status || "missing"}`);
      return;
    }
    applyCachedMobileConversation(cached);
  } else {
    mobileConversationState.revalidating = true;
    setMobileSearchStatus(
      t("mobile.restoringAndRefreshing", {}, "正在恢复并刷新"),
      "loading",
    );
    await loadMobileConversationSegment("initial");
    if (!mobileConversationState.messages.length) {
      await returnMobileHomeAfterRestoreFailure("reader_refresh_failed");
      return;
    }
  }
  restoreMobileReaderPosition(state);
  scheduleMobileRestoreStateSave({ interaction: false, delay: 0 });
}

async function initializeMobileStateRestore() {
  if (!isMobileSearchSurfaceActive()) return;
  const routeParams = new URLSearchParams(window.location.search);
  const routeConversationId = String(routeParams.get("mobile_conversation") || "").trim();
  const routeMessageId = String(routeParams.get("focus_message") || "").trim();
  const hasExplicitReaderRoute = Boolean(routeConversationId && routeMessageId);
  const cache = await mobilePrivateCacheReady;
  if (!cache) {
    restoreMobileConversationRoute();
    return;
  }
  const restored = await cache.loadResumeState();
  if (restored.status === "missing") {
    restoreMobileConversationRoute();
    return;
  }
  if (restored.status !== "ready") {
    if (
      hasExplicitReaderRoute
      && restored.reason === "cache_generation_changed"
    ) {
      restoreMobileConversationRoute();
      return;
    }
    await returnMobileHomeAfterRestoreFailure(restored.reason || restored.status);
    return;
  }
  const state = restored.state;
  if (
    hasExplicitReaderRoute
    && (
      state.route !== "reader"
      || state.currentConversationId !== routeConversationId
      || state.currentAnchorMessageId !== routeMessageId
    )
  ) {
    restoreMobileConversationRoute();
    return;
  }
  mobileStatePersistence.restoreInProgress = true;
  mobileStatePersistence.suppressInteractionUntil = Date.now() + 1500;
  mobileStatePersistence.restoredSavedAt = Number(state.savedAt) || 0;
  mobileStatePersistence.lastInteractionAt = Number(state.lastInteractionAt) || 0;
  mobileStatePersistence.hasUserInteraction = false;
  if (state.route === "results") await restoreMobileResultsState(state);
  else if (state.route === "reader") await restoreMobileReaderState(state);
  else setMobileView("home");
  mobileStatePersistence.restoreInProgress = false;
  mobileStatePersistence.suppressInteractionUntil = Date.now() + 1000;
}

function syncMobileVisualViewport() {
  if (!mobileShell) return;
  const visualViewport = window.visualViewport;
  const visualHeight = Math.max(1, visualViewport?.height ?? window.innerHeight);
  const visualWidth = Math.max(1, visualViewport?.width ?? window.innerWidth);
  const visualOffsetTop = Math.max(0, visualViewport?.offsetTop ?? 0);
  const visualOffsetLeft = Math.max(0, visualViewport?.offsetLeft ?? 0);
  if (!mobileSearchState.viewportBaseline) mobileSearchState.viewportBaseline = visualHeight;
  const inputFocused = document.activeElement === mobileSearchInput;
  if (!inputFocused) mobileSearchState.viewportBaseline = Math.max(mobileSearchState.viewportBaseline, visualHeight);
  const keyboardOpen = inputFocused && (
    mobileSearchState.viewportBaseline - visualHeight > 110
    || window.innerHeight - visualHeight > 110
  );
  if (keyboardOpen) {
    mobileSearchState.keyboardWasOpen = true;
    mobileSearchState.keyboardDismissedWhileFocused = false;
  } else if (inputFocused && mobileSearchState.keyboardWasOpen) {
    mobileSearchState.keyboardDismissedWhileFocused = true;
  } else if (!inputFocused) {
    mobileSearchState.keyboardWasOpen = false;
    mobileSearchState.keyboardDismissedWhileFocused = false;
  }
  mobileShell.style.setProperty("--yunos-mobile-visual-height", `${Math.round(visualHeight)}px`);
  mobileShell.style.setProperty("--yunos-mobile-visual-width", `${Math.round(visualWidth)}px`);
  mobileShell.style.setProperty("--yunos-mobile-visual-offset-top", `${Math.round(visualOffsetTop)}px`);
  mobileShell.style.setProperty("--yunos-mobile-visual-offset-left", `${Math.round(visualOffsetLeft)}px`);
  mobileShell.dataset.keyboardOpen = String(keyboardOpen);
  syncMobileSearchPosition();
}

function handleMobileSearchInput() {
  if (!mobileSearchInput) return;
  const query = mobileSearchInput.value.trim();
  const visibleCharacters = mobileVisibleCharacterCount(query);
  const queryChanged = query !== mobileSearchState.query;
  mobileSearchState.query = query;
  mobileSearchClear?.toggleAttribute("hidden", !mobileSearchInput.value);
  clearMobileSearchDebounce();
  if (queryChanged) {
    abortMobileSearchRequest({ invalidate: true });
    mobileSearchState.completedFirstPageKey = "";
  }

  if (visibleCharacters === 0) {
    resetMobileSearchResults();
    setMobileSearchHint("");
    setMobileView("home");
  } else if (visibleCharacters === 1) {
    resetMobileSearchResults({ keepQuery: true });
    setMobileSearchHint(t("search.enterOneMoreCharacter", {}, "再输入一个字开始搜索"));
    setMobileView("home");
  } else {
    resetMobileSearchResults({ keepQuery: true });
    setMobileSearchHint("");
    setMobileView("home");
    scheduleMobileSearch();
  }
  scheduleMobileRestoreStateSave({ interaction: true });
  syncMobileSearchPosition();
}

function initializeMobileSearch() {
  if (!mobileShell || !mobileSearchForm || !mobileSearchInput) return;
  const initialRouteParams = new URLSearchParams(window.location.search);
  if (
    String(initialRouteParams.get("mobile_conversation") || "").trim()
    && String(initialRouteParams.get("focus_message") || "").trim()
  ) {
    mobileConversationState.loadingInitial = true;
  }
  mobileRecentDrawer?.setAttribute("tabindex", "-1");
  if (mobileSortToggle) {
    mobileSortToggle.textContent = t("search.sortRelevance", {}, "相关度优先");
    mobileSortToggle.setAttribute(
      "aria-label",
      t("aria.currentRelevanceSort", {}, "当前相关度优先，点击切换排序"),
    );
  }
  syncMobileVisualViewport();

  mobileSearchInput.addEventListener("focus", () => {
    mobileSearchState.keyboardDismissedWhileFocused = false;
    syncMobileSearchPosition();
    syncMobileVisualViewport();
    window.requestAnimationFrame(syncMobileVisualViewport);
  });
  mobileSearchInput.addEventListener("blur", () => {
    window.setTimeout(syncMobileVisualViewport, 40);
  });
  mobileSearchInput.addEventListener("input", handleMobileSearchInput);
  mobileSearchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    clearMobileSearchDebounce();
    const visibleCharacters = mobileVisibleCharacterCount(mobileSearchInput.value);
    if (visibleCharacters < 2) {
      if (visibleCharacters === 1) {
        setMobileSearchHint(t("search.enterOneMoreCharacter", {}, "再输入一个字开始搜索"));
      } else {
        setMobileSearchHint(t("search.enterAtLeastTwo", {}, "请输入至少两个字"), "error");
      }
      return;
    }
    runMobileSearch({ reset: true }).catch(() => {});
    mobileSearchInput.blur();
  });

  mobileSearchClear?.addEventListener("click", () => {
    mobileSearchInput.value = "";
    handleMobileSearchInput();
    mobileSearchInput.blur();
  });
  mobileSortToggle?.addEventListener("click", () => {
    if (mobileSearchState.loading) abortMobileSearchRequest({ invalidate: true });
    mobileSearchState.sort = mobileSearchState.sort === "relevance" ? "latest" : "relevance";
    mobileSortToggle.textContent = mobileSearchState.sort === "relevance"
      ? t("search.sortRelevance", {}, "相关度优先")
      : t("search.sortLatest", {}, "最新优先");
    mobileSortToggle.setAttribute(
      "aria-label",
      t(
        "aria.currentSort",
        { sort: mobileSortToggle.textContent },
        `当前${mobileSortToggle.textContent}，点击切换排序`,
      ),
    );
    mobileSearchState.completedFirstPageKey = "";
    scheduleMobileRestoreStateSave({ interaction: true });
    runMobileSearch({ reset: true }).catch(() => {});
  });
  mobileLoadMore?.addEventListener("click", () => {
    if (mobileSearchState.paginationError) {
      mobileSearchState.autoLoadPaused = false;
      if (!mobileSearchState.groups.length) {
        runMobileSearch({ reset: true, force: true }).catch(() => {});
        return;
      }
    }
    loadNextMobileSearchPage({ force: true });
  });
  mobileSearchAllHost?.addEventListener("click", async () => {
    if (!isMobileHostAvailable()) return;
    const previousPosition = buildMobileRestoreState();
    mobileSearchState.completedFirstPageKey = "";
    await runMobileSearch({
      reset: true,
      force: true,
      forceOnline: true,
    });
    if (mobileSearchState.sourceMode === "online") {
      restoreMobileResultPosition(previousPosition);
      scheduleMobileRestoreStateSave({ interaction: true });
    }
  });

  mobileReaderBack?.addEventListener("click", returnFromMobileConversationReader);
  mobileReaderHideTopbar?.addEventListener("click", () => {
    setMobileReaderTopbarVisible(false, { remember: true });
    scheduleMobileRestoreStateSave({ interaction: true });
  });
  mobileReaderRestoreTopbar?.addEventListener("click", () => {
    setMobileReaderTopbarVisible(true, { remember: true });
    scheduleMobileRestoreStateSave({ interaction: true });
    mobileReaderHideTopbar?.focus({ preventScroll: true });
  });
  mobileReaderRevalidate?.addEventListener("click", reloadMobileConversationFromNetwork);
  mobileReaderScroll?.addEventListener("scroll", handleMobileReaderScroll, { passive: true });
  mobileResultsPage?.addEventListener("scroll", () => {
    mobileSearchState.resultsScrollTop = mobileResultsPage.scrollTop;
    window.clearTimeout(mobileStatePersistence.scrollTimer);
    mobileStatePersistence.scrollTimer = window.setTimeout(() => {
      mobileStatePersistence.scrollTimer = 0;
      scheduleMobileRestoreStateSave({ interaction: true, delay: 0 });
    }, MOBILE_SCROLL_SAVE_DEBOUNCE_MS);
  }, { passive: true });
  window.addEventListener("popstate", handleMobileReaderPopState);
  window.addEventListener("pagehide", () => {
    captureMobileReaderSnapshot();
    saveMobileRestoreState({ interaction: false }).catch(() => {});
    releaseActiveMobileReaderCache().catch(() => {});
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      saveMobileRestoreState({ interaction: false }).catch(() => {});
    }
  });

  if ("IntersectionObserver" in window && mobileSearchSentinel && mobileResultsPage) {
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      if (
        mobileSearchState.autoLoadPaused
        || mobileSearchState.paginationError
        || mobileSearchState.groups.length >= MOBILE_SEARCH_AUTO_LIMIT
      ) return;
      loadNextMobileSearchPage();
    }, {
      root: mobileResultsPage,
      rootMargin: "0px 0px 85% 0px",
      threshold: 0.01,
    });
    observer.observe(mobileSearchSentinel);
  }

  mobileRecentButton?.addEventListener("click", openMobileRecentDrawer);
  mobileRecentBackdrop?.addEventListener("click", () => closeMobileRecentDrawer());
  mobileRecentClear?.addEventListener("click", () => {
    writeMobileHistory({ searches: [], conversations: [] });
    renderMobileRecentDrawer();
  });

  let dragPointerId = null;
  let dragStartY = 0;
  let dragDistance = 0;
  mobileRecentDrawerHandle?.addEventListener("pointerdown", (event) => {
    dragPointerId = event.pointerId;
    dragStartY = event.clientY;
    dragDistance = 0;
    mobileRecentDrawerHandle.setPointerCapture?.(event.pointerId);
    mobileRecentDrawer?.classList.add("is-dragging");
  });
  mobileRecentDrawerHandle?.addEventListener("pointermove", (event) => {
    if (event.pointerId !== dragPointerId) return;
    dragDistance = Math.max(0, event.clientY - dragStartY);
    mobileRecentDrawer?.style.setProperty("--yunos-mobile-drawer-drag-y", `${dragDistance}px`);
  });
  const finishDrawerDrag = (event) => {
    if (event.pointerId !== dragPointerId) return;
    mobileRecentDrawerHandle?.releasePointerCapture?.(event.pointerId);
    mobileRecentDrawer?.classList.remove("is-dragging");
    dragPointerId = null;
    if (dragDistance >= 72) closeMobileRecentDrawer();
    else mobileRecentDrawer?.style.setProperty("--yunos-mobile-drawer-drag-y", "0px");
  };
  mobileRecentDrawerHandle?.addEventListener("pointerup", finishDrawerDrag);
  mobileRecentDrawerHandle?.addEventListener("pointercancel", finishDrawerDrag);

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (mobileShell?.dataset.mobileView === "reader") {
      event.preventDefault();
      returnFromMobileConversationReader();
      return;
    }
    if (mobileRecentDrawer?.dataset.open === "true") {
      event.preventDefault();
      closeMobileRecentDrawer();
    }
  });
  window.visualViewport?.addEventListener("resize", syncMobileVisualViewport);
  window.visualViewport?.addEventListener("scroll", syncMobileVisualViewport);
  window.addEventListener("resize", syncMobileVisualViewport);
  window.addEventListener("orientationchange", () => {
    syncMobileVisualViewport();
    scheduleMobileRestoreStateSave({ interaction: true });
  });
  window.addEventListener("yunos:host-connectivity", (event) => {
    if (mobileRecentDrawer?.dataset.open === "true") renderMobileRecentDrawer();
    syncMobileOfflineSurface(event.detail?.available);
  });
  window.addEventListener("online", async () => {
    renderMobileRecentDrawer();
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      syncMobileOfflineSurface(response.ok);
    } catch {
      syncMobileOfflineSurface(false);
    }
  });
  window.addEventListener("offline", () => {
    renderMobileRecentDrawer();
    syncMobileOfflineSurface(false);
  });
}

async function initializeMobilePrivateCacheCore() {
  if (!isMobileSearchSurfaceActive()) return null;
  document.body.dataset.yunosPrivateCacheState = "initializing";
  try {
    const cache = globalThis.yunosPrivateCache;
    if (!cache) throw new Error("Private cache core is unavailable.");
    await cache.open();
    document.body.dataset.yunosPrivateCacheState = "ready";
    window.dispatchEvent(new CustomEvent("yunos:private-cache-ready", {
      detail: {
        contentSchemaVersion: cache.constants.contentSchemaVersion,
        stores: [...cache.constants.dataStores],
      },
    }));
    return cache;
  } catch (error) {
    document.body.dataset.yunosPrivateCacheState = "unavailable";
    console.warn("YunOS private cache core initialization failed; continuing online-only.", error);
    return null;
  }
}

initializeMobileSearch();
const mobilePrivateCacheReady = initializeMobilePrivateCacheCore();
mobilePrivateCacheReady.then(() => {
  syncMobileOfflineSurface();
  initializeMobileStateRestore().catch((error) => {
    console.warn("YunOS mobile state restore failed closed.", {
      code: error?.code || error?.name || "restore_failed",
    });
    returnMobileHomeAfterRestoreFailure("restore_failed").catch(() => {});
  });
});

function splitTags(tags) {
  return text(tags)
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function appendTags(parent, tags) {
  const values = splitTags(tags);
  if (!values.length) {
    parent.appendChild(el(
      "span",
      "meta",
      t("legacy.core.tags.none", {}, "暂无标签"),
    ));
    return;
  }
  values.forEach((tag) => parent.appendChild(el("span", "tag", tag)));
}

function appendProjectTags(parent, values = []) {
  if (!values.length) {
    parent.appendChild(el(
      "span",
      "meta",
      t("legacy.core.projects.noMatches", {}, "暂无匹配项目"),
    ));
    return;
  }
  values.forEach((value) => parent.appendChild(el("span", "tag", value)));
}

function metaItem(label, value) {
  const item = el("span", "meta-item");
  item.appendChild(el("strong", "", label));
  item.appendChild(document.createTextNode(value || t("common.notRecorded", {}, "未记录")));
  return item;
}

function button(label, className = "button") {
  return el("button", className, label);
}

function makeKeyboardActivatable(element, activate, accessibleLabel = "") {
  element.tabIndex = 0;
  element.setAttribute("role", "link");
  if (accessibleLabel) element.setAttribute("aria-label", accessibleLabel);
  element.addEventListener("click", activate);
  element.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    activate();
  });
  return element;
}

function conversationCard(item, options = {}) {
  const card = el("article", "card clickable");
  const header = el("div", "card-heading");
  const conversationTitle = item.title || t("search.unnamedConversation", {}, "未命名对话");
  const updatedAt = item.update_time_iso
    ? formatUiDateTime(item.update_time_iso)
    : t("date.unknown", {}, "时间未知");
  const meta = el(
    "div",
    "meta",
    t(
      "legacy.core.conversation.meta",
      {
        time: updatedAt,
        messages: t(
          "units.messages",
          { count: formatUiNumber(item.message_count || 0) },
          "{count} 条消息",
        ),
        source: labelSourcePlatform(item.source_platform, item.source_platform_label),
      },
      "{time} · {messages} · 来源：{source}",
    ),
  );
  const cardTitle = el("h3", "card-title", conversationTitle);
  header.append(cardTitle, meta);
  const row = el("div", "row");
  if (item.status) row.appendChild(el("span", "status", labelStatus(item.status)));
  appendTags(row, item.tags);
  card.append(header, row);
  makeKeyboardActivatable(
    card,
    () => loadConversation(item.conversation_id, {
      fromProject: Boolean(options.fromProject),
      projectName: options.projectName || state.lastProject?.name || "",
      fromTimeline: Boolean(options.fromTimeline),
      timelineState: options.timelineState || null,
    }),
    t(
      "legacy.core.conversation.openAria",
      { title: conversationTitle },
      "打开对话：{title}",
    ),
  );
  return card;
}

async function loadRecent() {
  setActive("recent");
  const request = beginLegacyViewRequest("recent");
  setTitle(t("legacy.core.recent.title", {}, "最近对话"));
  content.replaceChildren(el(
    "div",
    "panel",
    t("legacy.core.recent.loading", {}, "正在加载最近对话…"),
  ));
  try {
    const data = await api("/api/recent");
    if (!isLegacyViewRequestCurrent(request)) return;
    content.innerHTML = "";
    if (!data.items.length) {
      return renderEmpty(t("legacy.core.recent.empty", {}, "没有找到最近对话。"));
    }
    data.items.forEach((item) => content.appendChild(conversationCard(item)));
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    renderTechnicalError(error);
  }
}

function renderSearch(prefill = state.lastSearchQuery, restoreResults = false) {
  setActive("search");
  setTitle(t("search.memory", {}, "搜索记忆"));
  content.innerHTML = "";
  appendReadonlyNotice();

  const panel = el("section", "panel");
  const row = el("div", "search-row");
  const input = el("input");
  input.id = "search-input";
  input.placeholder = t(
    "legacy.core.search.inputExample",
    {},
    "输入关键词，例如 锁鞋 或 YunOS",
  );
  input.autocomplete = "off";
  input.value = prefill || "";
  const sourceSelect = sourcePlatformSelect("search-source-platform", state.lastSearchSourcePlatform);
  const memorySelect = memorySpaceSelect("search-memory-space", state.lastSearchMemorySpace);
  const searchButton = button(t("search.memory", {}, "搜索记忆"));
  searchButton.id = "search-button";
  row.append(input, memorySelect, sourceSelect, searchButton);
  const message = el(
    "p",
    "form-message",
    t(
      "legacy.core.search.instructions",
      {},
      "按 Enter 或点击搜索。空关键词不会请求 API。",
    ),
  );
  message.id = "search-message";
  panel.append(row, message);

  const results = el("section", "content");
  results.id = "search-results";
  content.append(panel, results);

  searchButton.addEventListener("click", runSearch);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") runSearch();
  });
  if (restoreResults && state.lastSearchItems.length) {
    renderSearchResults(state.lastSearchItems, state.lastSearchQuery);
    setMessage(
      message,
      t(
        "legacy.core.search.restored",
        { count: formatUiNumber(state.lastSearchItems.length) },
        "已恢复 {count} 条搜索结果。",
      ),
    );
  }
  input.focus();
}

async function runSearch() {
  const input = document.querySelector("#search-input");
  const results = document.querySelector("#search-results");
  const message = document.querySelector("#search-message");
  const query = input.value.trim();
  const sourcePlatform = document.querySelector("#search-source-platform")?.value || "";
  const memorySpace = document.querySelector("#search-memory-space")?.value || "whale";
  if (!query) {
    state.lastSearchQuery = "";
    state.lastSearchSourcePlatform = sourcePlatform;
    state.lastSearchMemorySpace = memorySpace;
    state.lastSearchItems = [];
    renderEmpty(t("search.enterKeyword", {}, "请输入搜索关键词。"), results);
    setMessage(message, t("search.enterKeyword", {}, "请输入搜索关键词。"), "error");
    input.focus();
    return;
  }
  state.lastSearchQuery = query;
  state.lastSearchSourcePlatform = sourcePlatform;
  state.lastSearchMemorySpace = memorySpace;
  const request = beginLegacyViewRequest("search");
  setMessage(message, t("search.searching", {}, "正在搜索…"));
  results.replaceChildren(el("div", "panel", t("search.searching", {}, "正在搜索…")));
  try {
    const data = await api(
      `/api/search?q=${encodeURIComponent(query)}&limit=20${memoryParam(memorySpace)}${sourceParam(sourcePlatform)}`,
    );
    if (!isLegacyViewRequestCurrent(request)) return;
    state.lastSearchItems = data.items || [];
    renderSearchResults(state.lastSearchItems, query);
    setMessage(
      message,
      state.lastSearchItems.length
        ? t(
          "search.resultsFound",
          { count: formatUiNumber(state.lastSearchItems.length) },
          "找到 {count} 条结果。",
        )
        : t("search.noResults", {}, "没有找到结果。"),
    );
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    state.lastSearchItems = [];
    renderTechnicalError(
      error,
      results,
      "error.searchFailedRetry",
      "搜索失败，请重试。",
    );
    setTechnicalMessage(
      message,
      error,
      "error.searchFailedRetry",
      "搜索失败，请重试。",
    );
  }
}

function renderSearchResults(items, query) {
  const results = document.querySelector("#search-results");
  results.innerHTML = "";
  if (!items.length) {
    renderEmpty(t("search.noResults", {}, "没有找到结果。"), results);
    return;
  }
  const timelinePanel = el("section", "panel");
  const timelineActions = el("div", "actions");
  const timelineButton = button(
    t(
      "legacy.core.search.viewTimeline",
      { query },
      "查看“{query}”的时间轴",
    ),
    "button secondary",
  );
  timelineButton.addEventListener("click", () =>
    loadTimeline({ q: query, groupBy: "month", sourcePlatform: state.lastSearchSourcePlatform }),
  );
  timelineActions.appendChild(timelineButton);
  timelinePanel.append(
    el("h3", "section-title", t("search.keywordTimeline", {}, "关键词时间轴")),
    el(
      "p",
      "snippet",
      t(
        "legacy.core.search.timelineDescription",
        {},
        "按时间查看包含当前关键词的对话。",
      ),
    ),
    timelineActions,
  );
  results.appendChild(timelinePanel);
  items.forEach((item) => results.appendChild(searchResultCard(item, query)));
}

function searchResultCard(item, query, options = {}) {
  const fromProject = Boolean(options.fromProject);
  const projectName = options.projectName || state.lastProject?.name || "";
  const conversationOptions = {
    focusMessageId: item.message_id,
    fromSearch: !fromProject,
    fromProject,
    projectName,
  };
  const card = el("article", "card result-card");
  const top = el("div", "result-top");
  top.append(
    el(
      "span",
      "mode-pill organize-pill",
      t("legacy.core.search.organizeMode", {}, "整理模式"),
    ),
    el(
      "span",
      "status",
      t(
        "legacy.core.search.matchSource",
        { source: labelSource(item.source) },
        "命中来源：{source}",
      ),
    ),
    el(
      "span",
      "meta",
      item.update_time_iso || item.create_time_iso
        ? formatUiDateTime(item.update_time_iso || item.create_time_iso)
        : t("date.unknown", {}, "时间未知"),
    ),
  );

  const cardTitle = el("h3", "card-title");
  appendHighlighted(
    cardTitle,
    item.title || t("search.unnamedConversation", {}, "未命名对话"),
    query,
  );

  const meta = el("div", "meta-grid");
  meta.append(
    metaItem(
      t("legacy.core.label.role", {}, "角色："),
      item.role
        ? labelRole(item.role, item.role_label)
        : t("legacy.core.search.titleMatch", {}, "标题"),
    ),
    metaItem(
      t("legacy.core.label.time", {}, "时间："),
      item.create_time_iso || item.update_time_iso
        ? formatUiDateTime(item.create_time_iso || item.update_time_iso)
        : t("common.notRecorded", {}, "未记录"),
    ),
    metaItem(
      t("legacy.core.label.source", {}, "来源："),
      labelSourcePlatform(item.source_platform, item.source_platform_label),
    ),
    metaItem(
      t("legacy.core.label.memorySpace", {}, "记忆域："),
      memorySpaceLabels[item.memory_space] || item.memory_space || t("common.unknown", {}, "未知"),
    ),
    metaItem(t("legacy.core.label.status", {}, "状态："), labelStatus(item.status)),
    metaItem(
      t("legacy.core.label.matchedMessage", {}, "命中消息："),
      item.message_id || t("common.notRecorded", {}, "未记录"),
    ),
    metaItem(
      t("legacy.core.label.originalConversation", {}, "原文对话："),
      item.conversation_id || t("common.notRecorded", {}, "未记录"),
    ),
  );

  const tags = el("div", "row");
  appendTags(tags, item.tags);

  const snippet = el("button", "snippet snippet-link");
  appendHighlighted(snippet, item.snippet || "", query);
  snippet.title = t("legacy.core.action.viewOriginal", {}, "查看原文");
  snippet.disabled = !item.conversation_id;
  snippet.addEventListener("click", () => loadConversation(item.conversation_id, conversationOptions));

  const actions = el("div", "actions");
  const contextButton = button(
    t("legacy.core.action.viewContext", {}, "查看上下文"),
    "button secondary",
  );
  contextButton.disabled = !item.message_id || !item.conversation_id;
  contextButton.addEventListener("click", () => loadContext(item, query, { fromProject, projectName }));
  const sourceButton = button(
    t("legacy.core.action.viewOriginal", {}, "查看原文"),
    "button secondary",
  );
  sourceButton.disabled = !item.message_id;
  sourceButton.addEventListener("click", () => loadConversation(item.conversation_id, conversationOptions));
  const openButton = button(t("search.openConversation", {}, "打开完整对话"), "button secondary");
  openButton.addEventListener("click", () => loadConversation(item.conversation_id, conversationOptions));
  const copyButton = button(
    t("legacy.core.action.copyReference", {}, "复制引用"),
    "button secondary",
  );
  copyButton.disabled = !buildReference(item);
  copyButton.addEventListener("click", () => copyText(buildReference(item), copyButton));
  actions.append(contextButton, sourceButton, openButton, copyButton);

  card.append(top, cardTitle, meta, tags, snippet, actions);
  return card;
}

async function loadContext(item, query = state.lastSearchQuery, options = {}) {
  if (!item?.conversation_id || !item?.message_id) {
    renderEmpty(t(
      "error.sourceAnchorIncomplete",
      {},
      "Source Anchor 缺少 conversation_id 或 message_id。",
    ));
    return;
  }
  setActive("");
  const request = beginLegacyViewRequest("");
  setTitle(t("legacy.core.context.matchContext", {}, "命中上下文"));
  content.replaceChildren(el(
    "div",
    "panel",
    t("legacy.core.context.loading", {}, "正在加载命中上下文…"),
  ));
  try {
    const anchorParams = new URLSearchParams({
      conversation_id: item.conversation_id,
      message_id: item.message_id,
      window: "3",
    });
    const data = await api(`/api/message-context?${anchorParams.toString()}`);
    if (!isLegacyViewRequestCurrent(request)) return;
    state.lastContext = {
      data,
      item,
      query,
      fromProject: Boolean(options.fromProject),
      projectName: options.projectName || state.lastProject?.name || "",
    };
    renderContext(data, item, query, state.lastContext);
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    renderTechnicalError(error);
  }
}

function renderContext(data, item, query, options = {}) {
  const fromProject = Boolean(options.fromProject);
  const projectName = options.projectName || state.lastProject?.name || "";
  content.innerHTML = "";
  appendReadonlyNotice();
  const panel = el("section", "panel source-mode");
  const header = el("div", "detail-header");
  const titleBlock = el("div");
  titleBlock.append(
    el(
      "span",
      "mode-pill source-pill",
      t("legacy.core.source.originalMode", {}, "原文模式"),
    ),
    el(
      "h3",
      "card-title",
      data.conversation_title
        || item.title
        || t("search.unnamedConversation", {}, "未命名对话"),
    ),
    el(
      "p",
      "meta",
      t(
        "legacy.core.context.meta",
        {
          messageId: data.focus_message_id || t("common.notRecorded", {}, "未记录"),
          window: formatUiNumber(data.window),
        },
        "命中消息：{messageId} · 上下文窗口：{window}",
      ),
    ),
  );
  const actions = el("div", "actions");
  const openButton = button(t("search.openConversation", {}, "打开完整对话"));
  openButton.addEventListener("click", () =>
    loadConversation(data.conversation_id, {
      focusMessageId: data.focus_message_id,
      fromContext: true,
      fromProject,
      projectName,
    }),
  );
  const backButton = button(
    fromProject
      ? t("legacy.core.action.backProjectDetail", {}, "返回项目详情")
      : t("legacy.core.action.backSearchResults", {}, "返回搜索结果"),
    "button secondary",
  );
  backButton.addEventListener("click", () => {
    if (fromProject && projectName) {
      loadProject(projectName);
      return;
    }
    renderSearch(state.lastSearchQuery, true);
  });
  actions.append(openButton, backButton);
  header.append(titleBlock, actions);
  panel.appendChild(header);

  const messages = el("div", "message-list");
  data.messages.forEach((message) =>
    messages.appendChild(
      messageBlock(message, {
        query,
        collapsible: false,
        conversationId: data.conversation_id,
        conversationTitle: data.conversation_title || item.title || "",
        showActions: true,
        locateInConversation: true,
        fromProject,
        projectName,
      }),
    ),
  );
  panel.appendChild(messages);
  content.appendChild(panel);
}

async function loadConversation(id, options = {}) {
  const focusMessageId = options.focusMessageId || "";
  setActive("");
  const request = beginLegacyViewRequest("");
  setTitle(t("legacy.core.conversation.originalDetail", {}, "原文对话详情"));
  content.replaceChildren(el(
    "div",
    "panel",
    t("legacy.core.conversation.loadingFull", {}, "正在加载完整对话…"),
  ));
  try {
    const focusParam = focusMessageId ? `&focus_message_id=${encodeURIComponent(focusMessageId)}` : "";
    const data = await api(`/api/conversation?id=${encodeURIComponent(id)}${focusParam}`);
    if (!isLegacyViewRequestCurrent(request)) return;
    recordMobileRecentConversation(data.conversation || {});
    renderConversation(data.conversation, data.messages || [], {
      focusMessageId,
      fromAiDocument: Boolean(options.fromAiDocument),
      fromHomeSearch: Boolean(options.fromHomeSearch),
      fromSearch: Boolean(options.fromSearch),
      fromContext: Boolean(options.fromContext),
      fromProject: Boolean(options.fromProject),
      fromTimeline: Boolean(options.fromTimeline),
      timelineState: options.timelineState || null,
      projectName: options.projectName || state.lastProject?.name || "",
      query: state.lastSearchQuery,
    });
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    renderTechnicalError(error);
  }
}

function renderConversation(conv, messages, options = {}) {
  content.innerHTML = "";
  appendReadonlyNotice();
  state.selectedReference = null;
  state.lastConversationSource = {
    fromProject: Boolean(options.fromProject),
    projectName: options.projectName || state.lastProject?.name || "",
  };

  const header = el("section", "panel source-mode");
  const detailHeader = el("div", "detail-header");
  const titleBlock = el("div");
  const conversationTitle = conv.title
    || t("search.unnamedConversation", {}, "未命名对话");
  const conversationUpdatedAt = conv.update_time_iso
    ? formatUiDateTime(conv.update_time_iso)
    : t("date.unknown", {}, "时间未知");
  titleBlock.append(
    el(
      "span",
      "mode-pill source-pill",
      t("legacy.core.source.originalMode", {}, "原文模式"),
    ),
    el("h3", "card-title", conversationTitle),
    el(
      "p",
      "meta",
      t(
        "legacy.core.conversation.timeAndMessages",
        {
          time: conversationUpdatedAt,
          messages: t(
            "units.messages",
            { count: formatUiNumber(conv.message_count || messages.length) },
            "{count} 条消息",
          ),
        },
        "{time} · {messages}",
      ),
    ),
  );
  const headerActions = el("div", "actions");
  if (options.fromAiDocument) {
    const backAiDocumentButton = button(
      t("legacy.core.action.backAiResult", {}, "返回 AI 结果"),
      "button secondary",
    );
    backAiDocumentButton.addEventListener("click", returnToHomeAiDocument);
    headerActions.appendChild(backAiDocumentButton);
  }
  if (options.fromHomeSearch) {
    const backHomeSearchButton = button(
      t("legacy.core.action.backHomeSearch", {}, "返回首页搜索"),
      "button secondary",
    );
    backHomeSearchButton.addEventListener("click", returnToHomeMemorySearch);
    headerActions.appendChild(backHomeSearchButton);
  }
  if (options.fromSearch) {
    const backButton = button(
      t("legacy.core.action.backSearchResults", {}, "返回搜索结果"),
      "button secondary",
    );
    backButton.addEventListener("click", () => renderSearch(state.lastSearchQuery, true));
    headerActions.appendChild(backButton);
  }
  if (options.fromContext && state.lastContext) {
    const backContextButton = button(
      t("legacy.core.action.backContext", {}, "返回上下文"),
      "button secondary",
    );
    backContextButton.addEventListener("click", () =>
      renderContext(state.lastContext.data, state.lastContext.item, state.lastContext.query, state.lastContext),
    );
    headerActions.appendChild(backContextButton);
  }
  if (options.fromProject && options.projectName) {
    const backProjectButton = button(
      t("legacy.core.action.backProjectDetail", {}, "返回项目详情"),
      "button secondary",
    );
    backProjectButton.addEventListener("click", () => loadProject(options.projectName));
    headerActions.appendChild(backProjectButton);
  }
  if (options.fromTimeline) {
    const backTimelineButton = button(
      t("legacy.core.action.backTimeline", {}, "返回时间轴"),
      "button secondary",
    );
    backTimelineButton.addEventListener("click", () => loadTimeline(options.timelineState || state.lastTimeline));
    headerActions.appendChild(backTimelineButton);
  }
  detailHeader.append(titleBlock, headerActions);

  const statusRow = el("div", "row");
  statusRow.append(el(
    "span",
    "status",
    t(
      "legacy.core.conversation.currentStatus",
      { status: labelStatus(conv.status) },
      "当前状态：{status}",
    ),
  ));
  statusRow.append(el(
    "span",
    "status",
    t(
      "legacy.core.label.sourceValue",
      { source: labelSourcePlatform(conv.source_platform, conv.source_platform_label) },
      "来源：{source}",
    ),
  ));
  appendTags(statusRow, conv.tags);

  const noteBlock = el("div", "detail-block");
  noteBlock.append(
    el(
      "div",
      "label",
      t("legacy.core.conversation.statusNote", {}, "状态备注"),
    ),
    el(
      "p",
      "snippet",
      conv.note || t("legacy.core.conversation.noNote", {}, "暂无备注。"),
    ),
  );

  const warning = el(
    "div",
    state.readonly ? "readonly-notice inline-notice" : "real-write-warning",
    state.readonly
      ? t(
        "legacy.core.conversation.readonlyWriteDisabled",
        {},
        "真实库只读模式下禁止写入标签和状态。",
      )
      : t(
        "database.writeWarning",
        {},
        "警告：你正在使用真实本地记忆库，修改会写入真实数据。",
      ),
  );
  warning.classList.toggle("hidden", state.dbMode !== "real");

  const tagForm = el("div", "form-grid");
  const tagInput = el("input");
  tagInput.id = "tag-input";
  tagInput.placeholder = t(
    "legacy.core.tags.inputPlaceholder",
    {},
    "输入标签，多个标签用逗号分隔",
  );
  const tagButton = button(
    t("legacy.core.tags.add", {}, "添加标签"),
    "button secondary",
  );
  tagInput.disabled = state.readonly;
  tagButton.disabled = state.readonly;
  tagForm.append(tagInput, tagButton);

  const markForm = el("div", "form-grid mark-grid");
  const statusInput = el("select");
  statusInput.id = "status-input";
  statuses.forEach((status) => {
    const option = el("option", "", labelStatus(status));
    option.value = status;
    statusInput.appendChild(option);
  });
  statusInput.value = statuses.includes(conv.status) ? conv.status : "review";
  const noteInput = el("textarea");
  noteInput.id = "note-input";
  noteInput.placeholder = t("legacy.core.conversation.notePlaceholder", {}, "备注");
  noteInput.value = conv.note || "";
  const markButton = button(t("legacy.core.marks.saveStatus", {}, "保存状态"));
  statusInput.disabled = state.readonly;
  noteInput.disabled = state.readonly;
  markButton.disabled = state.readonly;
  markForm.append(statusInput, noteInput, markButton);

  const formMessage = el(
    "p",
    "form-message",
    state.readonly
      ? t(
        "legacy.core.conversation.readonlyWriteDisabled",
        {},
        "真实库只读模式下禁止写入标签和状态。",
      )
      : "",
  );
  header.append(detailHeader, statusRow, noteBlock, warning, tagForm, markForm, formMessage);
  content.appendChild(header);

  tagButton.addEventListener("click", async () => {
    const tags = tagInput.value.trim();
    if (!tags) {
      setMessage(
        formMessage,
        t("legacy.core.tags.atLeastOne", {}, "请输入至少一个标签。"),
        "error",
      );
      return;
    }
    try {
      const result = await api("/api/conversation/tag", {
        method: "POST",
        body: JSON.stringify({ conversation_id: conv.conversation_id, tags }),
      });
      setMessage(
        formMessage,
        t(
          "legacy.core.tags.savedTo",
          {
            database: result.db_mode === "real"
              ? t("database.real", {}, "真实记忆库")
              : t("legacy.core.database.test", {}, "测试库"),
          },
          "标签已保存到{database}。",
        ),
      );
      await loadConversation(conv.conversation_id, options);
    } catch (error) {
      setTechnicalMessage(formMessage, error);
    }
  });

  markButton.addEventListener("click", async () => {
    try {
      const result = await api("/api/conversation/mark", {
        method: "POST",
        body: JSON.stringify({
          conversation_id: conv.conversation_id,
          status: statusInput.value,
          note: noteInput.value,
        }),
      });
      setMessage(
        formMessage,
        t(
          "legacy.core.marks.statusSaved",
          { status: labelStatus(result.status) },
          "状态已保存为{status}。",
        ),
      );
      await loadConversation(conv.conversation_id, options);
    } catch (error) {
      setTechnicalMessage(formMessage, error);
    }
  });

  const referencePanel = el("section", "panel source-reference-panel");
  referencePanel.id = "source-reference-panel";
  content.appendChild(referencePanel);

  const messagePanel = el("section", "panel source-mode");
  messagePanel.appendChild(el(
    "h3",
    "section-title",
    t("legacy.core.conversation.originalMessages", {}, "原始消息记录"),
  ));
  const list = el("div", "message-list");
  messages.forEach((message) =>
    list.appendChild(
      messageBlock(message, {
        query: options.query || "",
        collapsible: true,
        conversationId: conv.conversation_id,
        conversationTitle: conv.title || "",
        showActions: true,
        fromProject: Boolean(options.fromProject),
        projectName: options.projectName || "",
      }),
    ),
  );
  messagePanel.appendChild(list);
  content.appendChild(messagePanel);

  const initialMessage =
    messages.find((message) => options.focusMessageId && message.message_id === options.focusMessageId) || messages[0];
  if (initialMessage) {
    const initialReference = buildMessageReference(initialMessage, conv.conversation_id, conv.title || "");
    state.selectedReference = initialReference;
    renderReferencePanel(initialReference);
    if (options.focusMessageId) {
      window.history.replaceState(null, "", `#${encodeURIComponent(options.focusMessageId)}`);
    }
  }

  if (options.focusMessageId) {
    window.setTimeout(() => {
      const focus = document.querySelector(`[data-message-id="${CSS.escape(options.focusMessageId)}"]`);
      if (focus) {
        focus.classList.add("locating-message", "selected-message");
        focus.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }, 80);
  }
}

function focusMessageInPage(messageId) {
  const target = document.querySelector(`[data-message-id="${CSS.escape(messageId)}"]`);
  if (!target) return;
  document.querySelectorAll(".locating-message").forEach((node) => node.classList.remove("locating-message"));
  document.querySelectorAll(".selected-message").forEach((node) => node.classList.remove("selected-message"));
  target.classList.add("locating-message");
  target.scrollIntoView({ behavior: "smooth", block: "center" });
}

function buildMessageReference(message, conversationId = "", conversationTitle = "") {
  const convId = message.conversation_id || conversationId;
  return {
    conversation_id: convId,
    conversation_title: conversationTitle,
    message_id: message.message_id || "",
    role: message.role || "",
    role_label: labelRole(message.role, message.role_label),
    create_time_iso: message.create_time_iso || "",
    db_mode: state.dbMode,
    reference_uri: buildReference({
      conversation_id: convId,
      message_id: message.message_id || "",
      reference_uri: message.reference_uri || "",
    }),
  };
}

function selectOriginalMessage(message, options = {}) {
  const reference = buildMessageReference(message, options.conversationId, options.conversationTitle);
  state.selectedReference = reference;
  if (message.message_id) {
    focusMessageInPage(message.message_id);
    const selected = document.querySelector(`[data-message-id="${CSS.escape(message.message_id)}"]`);
    if (selected) selected.classList.add("selected-message");
    window.history.replaceState(null, "", `#${encodeURIComponent(message.message_id)}`);
  }
  renderReferencePanel(reference);
}

function renderReferencePanel(reference) {
  const panel = document.querySelector("#source-reference-panel");
  if (!panel) return;
  panel.innerHTML = "";
  const header = el("div", "detail-header");
  const titleBlock = el("div");
  titleBlock.append(
    el(
      "span",
      "mode-pill source-pill",
      t("legacy.core.source.originalMode", {}, "原文模式"),
    ),
    el(
      "h3",
      "section-title",
      t("legacy.core.source.originalReference", {}, "原文引用"),
    ),
  );
  const actions = el("div", "actions");
  const copyButton = button(
    t("legacy.core.action.copyReference", {}, "复制引用"),
    "button secondary",
  );
  copyButton.disabled = !reference.reference_uri;
  copyButton.addEventListener("click", () => copyText(reference.reference_uri, copyButton));
  const contextButton = button(
    t("legacy.core.action.viewContext", {}, "查看上下文"),
    "button secondary",
  );
  contextButton.disabled = !reference.message_id || !reference.conversation_id;
  contextButton.addEventListener("click", () =>
    loadContext(
      {
        conversation_id: reference.conversation_id,
        message_id: reference.message_id,
        title: reference.conversation_title,
        reference_uri: reference.reference_uri,
      },
      state.lastSearchQuery,
      state.lastConversationSource,
    ),
  );
  const returnButton = button(
    t("legacy.core.action.backFullConversation", {}, "返回完整对话"),
    "button secondary",
  );
  returnButton.disabled = !reference.message_id;
  returnButton.addEventListener("click", () => focusMessageInPage(reference.message_id));
  actions.append(copyButton, contextButton, returnButton);
  header.append(titleBlock, actions);

  const grid = el("div", "reference-grid");
  grid.append(
    metaItem(
      t("legacy.core.label.selectedMessage", {}, "当前选中消息："),
      reference.message_id || t("common.notRecorded", {}, "未记录"),
    ),
    metaItem(
      t("legacy.core.label.conversationId", {}, "对话 ID："),
      reference.conversation_id || t("common.notRecorded", {}, "未记录"),
    ),
    metaItem(
      t("legacy.core.label.messageId", {}, "消息 ID："),
      reference.message_id || t("common.notRecorded", {}, "未记录"),
    ),
    metaItem(
      t("legacy.core.label.role", {}, "角色："),
      reference.role_label || t("common.unknown", {}, "未知"),
    ),
    metaItem(
      t("legacy.core.label.time", {}, "时间："),
      reference.create_time_iso
        ? formatUiDateTime(reference.create_time_iso)
        : t("common.notRecorded", {}, "未记录"),
    ),
    metaItem(
      t("legacy.core.label.currentDatabaseMode", {}, "当前数据库模式："),
      labelDbMode(reference.db_mode),
    ),
  );
  const uri = el(
    "p",
    "reference-uri",
    reference.reference_uri
      || t("legacy.core.source.noReference", {}, "无引用"),
  );
  panel.append(header, grid, uri);
}

async function copyText(
  value,
  trigger,
  doneText = t("common.copied", {}, "已复制"),
) {
  if (!value) return;
  let copied = false;
  try {
    await navigator.clipboard.writeText(value);
    copied = true;
  } catch {}
  if (!copied) {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    textarea.remove();
  }
  if (copied && trigger) {
    const oldText = trigger.textContent;
    trigger.textContent = doneText;
    window.setTimeout(() => {
      trigger.textContent = oldText;
    }, 1200);
    return;
  }
  if (!copied) {
    window.prompt(
      t("legacy.core.action.copyReferencePrompt", {}, "复制引用："),
      value,
    );
  }
}

function messageBlock(message, options = {}) {
  const query = options.query || "";
  const collapsible = options.collapsible !== false;
  const block = el("div", message.is_focus ? "message focus-message" : "message");
  block.dataset.messageId = message.message_id || "";
  if (message.message_id) block.id = `message-${message.message_id}`;
  const row = el("div", "row");
  row.append(
    el("span", "message-role", labelRole(message.role, message.role_label)),
    el(
      "span",
      "meta",
      message.create_time_iso
        ? formatUiDateTime(message.create_time_iso)
        : t("date.unknown", {}, "时间未知"),
    ),
  );
  const body = el("p", "message-text");
  body.title = t("legacy.core.action.locateOriginal", {}, "定位原文");
  body.tabIndex = 0;
  const locateOriginal = () => {
    if (options.locateInConversation) {
      loadConversation(message.conversation_id || options.conversationId, {
        focusMessageId: message.message_id,
        fromContext: true,
        fromProject: Boolean(options.fromProject),
        projectName: options.projectName || "",
      });
      return;
    }
    selectOriginalMessage(message, options);
  };
  body.addEventListener("click", locateOriginal);
  body.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      locateOriginal();
    }
  });
  const full = text(message.text);
  const truncated = collapsible && full.length > 800;
  const render = (expanded) => {
    body.innerHTML = "";
    appendHighlighted(body, truncated && !expanded ? `${full.slice(0, 800)}...` : full, query);
  };
  render(false);
  block.append(row, body);
  if (truncated) {
    const expandButton = button(t("common.expand", {}, "展开"), "button secondary small");
    let expanded = false;
    expandButton.addEventListener("click", () => {
      expanded = !expanded;
      render(expanded);
      expandButton.textContent = expanded
        ? t("common.collapse", {}, "收起")
        : t("common.expand", {}, "展开");
    });
    block.appendChild(expandButton);
  }
  if (options.showActions && message.message_id) {
    const actions = el("div", "message-actions");
    const contextButton = button(
      t("legacy.core.action.viewContext", {}, "查看上下文"),
      "button secondary small",
    );
    contextButton.addEventListener("click", () =>
      loadContext(
        {
          message_id: message.message_id,
          conversation_id: options.conversationId,
          title: options.conversationTitle,
        },
        query,
        {
          fromProject: Boolean(options.fromProject),
          projectName: options.projectName || "",
        },
      ),
    );
    const copyButton = button(
      t("legacy.core.action.copyReference", {}, "复制引用"),
      "button secondary small",
    );
    copyButton.addEventListener("click", () =>
      copyText(
        buildReference({
          conversation_id: message.conversation_id || options.conversationId,
          message_id: message.message_id,
          reference_uri: message.reference_uri,
        }),
        copyButton,
      ),
    );
    const locateButton = button(
      t("legacy.core.action.locateMessage", {}, "定位此消息"),
      "button secondary small",
    );
    locateButton.addEventListener("click", locateOriginal);
    actions.append(contextButton, copyButton, locateButton);
    block.appendChild(actions);
  }
  return block;
}

async function loadTimeline(options = {}) {
  const timelineState = {
    groupBy: options.groupBy || options.group_by || state.lastTimeline.groupBy || "month",
    project: options.project || "",
    q: options.q || "",
    sourcePlatform: options.sourcePlatform || options.source_platform || state.lastTimeline.sourcePlatform || "",
  };
  setActive("timeline");
  const request = beginLegacyViewRequest("timeline");
  setTitle(t("nav.timeline", {}, "时间轴"));
  content.replaceChildren(el(
    "div",
    "panel",
    t("legacy.core.timeline.loading", {}, "正在加载时间轴…"),
  ));
  try {
    const projectsData = await api("/api/projects");
    if (!isLegacyViewRequestCurrent(request)) return;
    const params = new URLSearchParams();
    params.set("group_by", timelineState.groupBy);
    params.set("limit", "12");
    if (timelineState.project) params.set("project", timelineState.project);
    if (timelineState.q) params.set("q", timelineState.q);
    if (timelineState.sourcePlatform) params.set("source_platform", timelineState.sourcePlatform);
    const data = await api(`/api/timeline?${params.toString()}`);
    if (!isLegacyViewRequestCurrent(request)) return;
    state.lastTimeline = {
      groupBy: data.group_by || timelineState.groupBy,
      project: data.project || timelineState.project,
      q: data.q || timelineState.q,
      sourcePlatform: data.source_platform || timelineState.sourcePlatform,
      items: data.items || [],
    };
    renderTimeline(data, projectsData.items || []);
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    renderTechnicalError(error);
  }
}

function renderTimeline(data, projects = []) {
  content.innerHTML = "";
  appendReadonlyNotice();

  const controls = el("section", "panel timeline-controls");
  const controlGrid = el("div", "timeline-control-grid");
  const groupSelect = el("select");
  groupSelect.id = "timeline-group";
  [
    ["month", t("legacy.core.timeline.byMonth", {}, "按月")],
    ["day", t("legacy.core.timeline.byDay", {}, "按日")],
  ].forEach(([value, label]) => {
    const option = el("option", "", label);
    option.value = value;
    groupSelect.appendChild(option);
  });
  groupSelect.value = data.group_by || "month";

  const projectSelect = el("select");
  projectSelect.id = "timeline-project";
  const allOption = el(
    "option",
    "",
    t("legacy.core.projects.all", {}, "全部项目"),
  );
  allOption.value = "";
  projectSelect.appendChild(allOption);
  projects.forEach((project) => {
    const option = el("option", "", project.name);
    option.value = project.name;
    projectSelect.appendChild(option);
  });
  projectSelect.value = data.project || "";

  const qInput = el("input");
  qInput.id = "timeline-query";
  qInput.placeholder = t(
    "legacy.core.timeline.keywordPlaceholder",
    {},
    "关键词筛选，可留空",
  );
  qInput.value = data.q || "";
  const sourceSelect = sourcePlatformSelect("timeline-source-platform", data.source_platform || "");
  const applyButton = button(t("legacy.core.timeline.update", {}, "更新时间轴"));
  const clearButton = button(
    t("legacy.core.timeline.clearFilters", {}, "清除筛选"),
    "button secondary",
  );

  const apply = () =>
    loadTimeline({
      groupBy: groupSelect.value,
      project: projectSelect.value,
      q: qInput.value.trim(),
      sourcePlatform: sourceSelect.value,
    });
  applyButton.addEventListener("click", apply);
  groupSelect.addEventListener("change", apply);
  projectSelect.addEventListener("change", apply);
  qInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") apply();
  });
  sourceSelect.addEventListener("change", apply);
  clearButton.addEventListener("click", () => loadTimeline({ groupBy: "month", project: "", q: "", sourcePlatform: "" }));

  controlGrid.append(
    labeledControl(
      t("legacy.core.label.databaseMode", {}, "数据库模式"),
      labelDbMode(data.db_mode || state.dbMode),
    ),
    labeledControl(
      t("legacy.core.timeline.grouping", {}, "分组方式"),
      groupSelect,
    ),
    labeledControl(
      t("legacy.core.timeline.projectFilter", {}, "项目筛选"),
      projectSelect,
    ),
    labeledControl(
      t("legacy.core.timeline.sourceFilter", {}, "来源筛选"),
      sourceSelect,
    ),
    labeledControl(
      t("legacy.core.timeline.keywordFilter", {}, "关键词筛选"),
      qInput,
    ),
  );
  const actions = el("div", "actions");
  actions.append(applyButton, clearButton);
  controls.append(
    el(
      "h3",
      "section-title",
      t("legacy.core.timeline.filters", {}, "时间轴筛选"),
    ),
    el(
      "p",
      "snippet",
      t(
        "legacy.core.timeline.description",
        {},
        "按时间查看记忆活动。筛选只读取当前数据库，不保存任何结果。",
      ),
    ),
    controlGrid,
    actions,
  );
  content.appendChild(controls);

  if (!data.items || !data.items.length) {
    content.appendChild(el(
      "div",
      "empty-card",
      t("legacy.core.timeline.noResults", {}, "没有找到时间轴结果。"),
    ));
    return;
  }
  data.items.forEach((item) => content.appendChild(timelinePeriodCard(item, state.lastTimeline)));
}

function labeledControl(label, control) {
  const wrap = el("label", "control-label");
  wrap.appendChild(el("span", "", label));
  if (typeof control === "string") {
    wrap.appendChild(el("strong", "control-value", control));
  } else {
    wrap.appendChild(control);
  }
  return wrap;
}

function timelinePeriodCard(item, timelineState) {
  const card = el("article", "panel timeline-period");
  const header = el("div", "detail-header");
  const titleBlock = el("div");
  titleBlock.append(
    el(
      "h3",
      "card-title",
      item.period || t("date.unknown", {}, "时间未知"),
    ),
    el(
      "p",
      "meta",
      t(
        "legacy.core.timeline.periodCounts",
        {
          conversations: formatUiNumber(item.conversation_count || 0),
          messages: formatUiNumber(item.message_count || 0),
        },
        "对话数：{conversations} · 消息数：{messages}",
      ),
    ),
  );
  const sourceText = sourceCountsText(item.source_counts);
  if (sourceText) titleBlock.appendChild(el("p", "meta", sourceText));
  const projectRow = el("div", "row");
  if (item.top_projects && item.top_projects.length) {
    item.top_projects.forEach((project) =>
      projectRow.appendChild(el(
        "span",
        "tag",
        `${project.name} ${formatUiNumber(project.match_count || 0)}`,
      )),
    );
  } else {
    projectRow.appendChild(el(
      "span",
      "meta",
      t("legacy.core.timeline.noRelatedProjects", {}, "相关项目：暂无"),
    ));
  }
  header.append(titleBlock, projectRow);
  card.appendChild(header);

  const list = el("div", "timeline-list");
  (item.conversations || []).forEach((conversation) => {
    const row = el("article", "timeline-conversation clickable");
    const titleButton = el(
      "button",
      "timeline-title",
      conversation.title || t("search.unnamedConversation", {}, "未命名对话"),
    );
    titleButton.addEventListener("click", () =>
      loadConversation(conversation.conversation_id, {
        fromTimeline: true,
        timelineState,
      }),
    );
    const meta = el(
      "p",
      "meta",
      t(
        "legacy.core.conversation.meta",
        {
          time: conversation.update_time_iso
            ? formatUiDateTime(conversation.update_time_iso)
            : t("date.unknown", {}, "时间未知"),
          messages: t(
            "units.messages",
            { count: formatUiNumber(conversation.message_count || 0) },
            "{count} 条消息",
          ),
          source: labelSourcePlatform(
            conversation.source_platform,
            conversation.source_platform_label,
          ),
        },
        "{time} · {messages} · 来源：{source}",
      ),
    );
    const matches = el("div", "row");
    appendProjectTags(matches, conversation.project_matches || []);
    const actions = el("div", "actions");
    const openButton = button(
      t("legacy.core.action.openOriginal", {}, "打开原文"),
      "button secondary small",
    );
    openButton.addEventListener("click", () =>
      loadConversation(conversation.conversation_id, {
        fromTimeline: true,
        timelineState,
      }),
    );
    const projectButton = button(
      t("legacy.core.action.viewProject", {}, "查看项目"),
      "button secondary small",
    );
    const firstProject = (conversation.project_matches || [])[0] || "";
    projectButton.disabled = !firstProject;
    projectButton.addEventListener("click", () => loadProject(firstProject));
    actions.append(openButton, projectButton);
    row.append(titleButton, meta, matches, actions);
    list.appendChild(row);
  });
  card.appendChild(list);
  return card;
}

async function loadSummaries(options = {}) {
  const memorySpace = options.memorySpace || state.lastSummaries.memorySpace || "whale";
  const summaryType = options.summaryType ?? state.lastSummaries.summaryType ?? "";
  setActive("summaries");
  const request = beginLegacyViewRequest("summaries");
  setTitle(t("nav.memorySummaries", {}, "记忆摘要"));
  content.replaceChildren(el(
    "div",
    "panel",
    t("legacy.core.summaries.loading", {}, "正在加载记忆摘要…"),
  ));
  try {
    const params = new URLSearchParams();
    params.set("memory_space", memorySpace);
    params.set("limit", "20");
    if (summaryType) params.set("summary_type", summaryType);
    const summariesRequest = api(`/api/memory-summaries?${params.toString()}`);
    const healthRequest = api(`/api/memory-summary-health?${params.toString()}`).catch((error) => ({
      error: error.message,
      items: [],
      overall: null,
    }));
    const data = await summariesRequest;
    if (!isLegacyViewRequestCurrent(request)) return;
    const health = await healthRequest;
    if (!isLegacyViewRequestCurrent(request)) return;
    state.lastSummaries = {
      memorySpace: data.memory_space || memorySpace,
      summaryType: data.summary_type || summaryType,
      items: data.items || [],
      health,
      healthError: health.error || "",
    };
    renderSummaries(data, health);
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    renderTechnicalError(error);
  }
}

function renderSummaries(data, health = null) {
  content.innerHTML = "";
  appendReadonlyNotice();
  const controls = el("section", "panel summary-controls");
  const controlGrid = el("div", "timeline-control-grid");
  const memorySelect = memorySpaceSelect("summary-memory-space", data.memory_space || "whale");
  const typeSelect = summaryTypeSelect("summary-type", data.summary_type || "");
  const applyButton = button(
    t("legacy.core.summaries.view", {}, "查看摘要"),
  );
  const apply = () => loadSummaries({ memorySpace: memorySelect.value, summaryType: typeSelect.value });
  applyButton.addEventListener("click", apply);
  memorySelect.addEventListener("change", apply);
  typeSelect.addEventListener("change", apply);
  controlGrid.append(
    labeledControl(
      t("legacy.core.label.memorySpacePlain", {}, "记忆空间"),
      memorySelect,
    ),
    labeledControl(
      t("legacy.core.summaries.type", {}, "摘要类型"),
      typeSelect,
    ),
    labeledControl(
      t("legacy.core.label.databaseMode", {}, "数据库模式"),
      labelDbMode(data.db_mode || state.dbMode),
    ),
    labeledControl(
      t("legacy.core.summaries.readonlyStatus", {}, "只读状态"),
      data.readonly
        ? t("legacy.core.database.readonly", {}, "只读")
        : t("legacy.core.database.currentMode", {}, "当前模式"),
    ),
  );
  controls.append(
    el("h3", "section-title", t("nav.memorySummaries", {}, "记忆摘要")),
    el(
      "p",
      "snippet",
      t(
        "legacy.core.summaries.description",
        {},
        "只读查看 memory_summaries 中已有摘要；这里不会生成、刷新或写入摘要。",
      ),
    ),
    controlGrid,
    applyButton,
  );
  content.appendChild(controls);
  content.appendChild(summaryHealthOverview(health));

  if (!data.items || !data.items.length) {
    content.appendChild(el(
      "div",
      "empty-card",
      t("legacy.core.summaries.empty", {}, "当前没有摘要记录。"),
    ));
    return;
  }
  const healthById = new Map((health?.items || []).map((item) => [String(item.summary_id), item]));
  data.items.forEach((item) => content.appendChild(summaryCard(item, healthById.get(String(item.id)))));
}

function summaryHealthOverview(health) {
  const card = el("section", "panel summary-health-overview");
  if (health?.error) {
    card.append(
      el(
        "h3",
        "section-title",
        t("legacy.core.summaries.health", {}, "摘要健康诊断"),
      ),
      el(
        "p",
        "form-message error",
        t(
          "legacy.core.summaries.healthUnavailable",
          { detail: health.error },
          "健康诊断暂不可用：{detail}",
        ),
      ),
    );
    return card;
  }
  const overall = health?.overall || {};
  const grid = el("div", "summary-health-grid");
  grid.append(
    healthMetric(
      t("legacy.core.summaries.count", {}, "摘要数量"),
      formatUiNumber(overall.total_count ?? 0),
    ),
    healthMetric(
      t("legacy.core.summaries.averageHealth", {}, "平均健康分"),
      formatUiNumber(overall.average_health_score ?? 0),
    ),
    healthMetric(
      t("legacy.core.summaries.warningCount", {}, "警告数"),
      formatUiNumber(overall.warning_count ?? 0),
    ),
    healthMetric(
      t("legacy.core.summaries.sourceMessageCount", {}, "来源消息数"),
      formatUiNumber(overall.total_source_message_count ?? 0),
    ),
  );
  card.append(
    el(
      "h3",
      "section-title",
      t("legacy.core.summaries.health", {}, "摘要健康诊断"),
    ),
    grid,
  );
  return card;
}

function healthMetric(label, value) {
  const item = el("div", "health-metric");
  item.append(el("span", "health-metric-label", label), el("strong", "", String(value)));
  return item;
}

function summaryCard(item, health = null) {
  const card = el("article", "card summary-card");
  const conversationId = item.conversation_id || "";
  const shortConversation =
    conversationId.length > 18 ? `${conversationId.slice(0, 8)}...${conversationId.slice(-6)}` : conversationId;
  const header = el("div", "card-heading");
  header.append(
    el("h3", "card-title", labelSummaryType(item.summary_type)),
    el(
      "p",
      "meta",
      t(
        "legacy.core.summaries.cardMeta",
        {
          memorySpace: memorySpaceLabels[item.memory_space]
            || item.memory_space
            || t("common.unknown", {}, "未知"),
          time: item.created_at
            ? formatUiDateTime(item.created_at)
            : t("date.unknown", {}, "时间未知"),
        },
        "{memorySpace} · {time}",
      ),
    ),
  );
  const meta = el("div", "meta-grid");
  meta.append(
    metaItem(
      t("legacy.core.label.memorySpace", {}, "记忆域："),
      memorySpaceLabels[item.memory_space]
        || item.memory_space
        || t("common.unknown", {}, "未知"),
    ),
    metaItem(
      t("legacy.core.summaries.typeLabel", {}, "摘要类型："),
      labelSummaryType(item.summary_type),
    ),
    metaItem(
      t("legacy.core.summaries.conversationLabel", {}, "对话："),
      shortConversation || t("common.notRecorded", {}, "未记录"),
    ),
    metaItem(
      t("legacy.core.summaries.messageCountLabel", {}, "消息数："),
      formatUiNumber(item.source_message_count || 0),
    ),
    metaItem(
      t("legacy.core.summaries.methodLabel", {}, "方法："),
      item.method || "extractive_v1",
    ),
    metaItem(
      t("legacy.core.summaries.modelLabel", {}, "模型："),
      item.model_name || t("common.notRecorded", {}, "未记录"),
    ),
  );
  const body = el("p", "summary-text", item.summary_text || "");
  card.append(header, meta, summaryHealthBlock(health), body);
  return card;
}

function summaryHealthBlock(health) {
  const block = el("div", "summary-health-block");
  if (!health) {
    block.append(el(
      "span",
      "health-badge muted",
      t("legacy.core.summaries.healthMissing", {}, "健康诊断未返回"),
    ));
    return block;
  }
  block.append(el(
    "span",
    "health-badge",
    t(
      "legacy.core.summaries.healthScore",
      { score: formatUiNumber(health.health_score) },
      "健康分 {score}",
    ),
  ));
  const warnings = health.warnings || [];
  if (!warnings.length) {
    block.append(el(
      "span",
      "health-pass",
      t("legacy.core.summaries.structurePassed", {}, "结构检查通过"),
    ));
    return block;
  }
  const list = el("ul", "summary-warning-list");
  warnings.forEach((warning) => list.appendChild(el("li", "", warning)));
  block.append(list);
  return block;
}

async function loadMarks() {
  setActive("marks");
  const request = beginLegacyViewRequest("marks");
  setTitle(t("nav.statusManagement", {}, "状态管理"));
  content.replaceChildren(el(
    "div",
    "panel",
    t("legacy.core.marks.loading", {}, "正在加载状态…"),
  ));
  try {
    const data = await api("/api/marks");
    if (!isLegacyViewRequestCurrent(request)) return;
    content.innerHTML = "";
    if (!data.items.length) {
      return renderEmpty(
        t("legacy.core.marks.empty", {}, "没有已标记的对话。"),
      );
    }
    data.items.forEach((item) => {
      const card = el("article", "card clickable");
      card.append(
        el("div", "row"),
        el(
          "h3",
          "card-title",
          item.title || t("search.unnamedConversation", {}, "未命名对话"),
        ),
        el(
          "p",
          "snippet",
          item.note || t("legacy.core.conversation.noNote", {}, "暂无备注。"),
        ),
      );
      card.querySelector(".row").append(el("span", "status", labelStatus(item.status)), el("span", "meta", item.updated_at || ""));
      makeKeyboardActivatable(
        card,
        () => loadConversation(item.conversation_id),
        t(
          "legacy.core.marks.openAria",
          {
            title: item.title
              || t("search.unnamedConversation", {}, "未命名对话"),
          },
          "打开状态记录：{title}",
        ),
      );
      content.appendChild(card);
    });
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    renderTechnicalError(error);
  }
}

async function loadTags() {
  setActive("tags");
  const request = beginLegacyViewRequest("tags");
  setTitle(t("nav.tagManagement", {}, "标签管理"));
  content.replaceChildren(el(
    "div",
    "panel",
    t("legacy.core.tags.loading", {}, "正在加载标签…"),
  ));
  try {
    const data = await api("/api/tags");
    if (!isLegacyViewRequestCurrent(request)) return;
    content.innerHTML = "";
    if (!data.items.length) {
      return renderEmpty(t("legacy.core.tags.empty", {}, "没有标签。"));
    }
    const panel = el("section", "panel");
    const row = el("div", "row");
    data.items.forEach((item) => {
      const tagButton = button(
        `${item.tag} (${formatUiNumber(item.conversation_count || 0)})`,
        "nav-item tag-button",
      );
      tagButton.addEventListener("click", () => loadTag(item.tag));
      row.appendChild(tagButton);
    });
    panel.appendChild(row);
    content.appendChild(panel);
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    renderTechnicalError(error);
  }
}

async function loadTag(tag) {
  setActive("tags");
  const request = beginLegacyViewRequest("tags");
  setTitle(t("legacy.core.tags.namedTitle", { tag }, "标签：{tag}"));
  content.replaceChildren(el(
    "div",
    "panel",
    t("legacy.core.tags.loadingConversations", {}, "正在加载标签对话…"),
  ));
  try {
    const data = await api(`/api/tag?name=${encodeURIComponent(tag)}`);
    if (!isLegacyViewRequestCurrent(request)) return;
    content.innerHTML = "";
    if (!data.items.length) {
      return renderEmpty(t("search.noResults", {}, "没有找到结果。"));
    }
    data.items.forEach((item) => content.appendChild(conversationCard(item)));
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    renderTechnicalError(error);
  }
}

async function loadProjects(sourcePlatform = state.lastProjectSourcePlatform || "") {
  setActive("projects");
  const request = beginLegacyViewRequest("projects");
  setTitle(t("legacy.core.projects.view", {}, "项目视图"));
  content.replaceChildren(el(
    "div",
    "panel",
    t("legacy.core.projects.loading", {}, "正在加载项目视图…"),
  ));
  try {
    state.lastProjectSourcePlatform = sourcePlatform || "";
    const data = await api(`/api/projects?source_platform=${encodeURIComponent(state.lastProjectSourcePlatform)}`);
    if (!isLegacyViewRequestCurrent(request)) return;
    content.innerHTML = "";
    appendReadonlyNotice();
    const intro = el("section", "panel");
    const controls = el("div", "search-row");
    const sourceSelect = sourcePlatformSelect("project-source-platform", state.lastProjectSourcePlatform);
    const applyButton = button(
      t("legacy.core.projects.update", {}, "更新项目视图"),
    );
    applyButton.addEventListener("click", () => loadProjects(sourceSelect.value));
    sourceSelect.addEventListener("change", () => loadProjects(sourceSelect.value));
    controls.append(sourceSelect, applyButton);
    intro.append(
      el(
        "h3",
        "section-title",
        t("legacy.core.projects.lightweightView", {}, "轻量项目视图"),
      ),
      el(
        "p",
        "snippet",
        state.readonly
          ? t(
            "legacy.core.projects.readonlyDescription",
            {},
            "当前项目视图以只读方式读取真实库，使用关键词规则临时聚合，不保存项目匹配结果。",
          )
          : t(
            "legacy.core.projects.testDescription",
            {},
            "当前项目视图基于当前测试库内容和关键词规则聚合，不写项目配置。",
          ),
      ),
      controls,
    );
    content.appendChild(intro);
    if (!data.items.length) {
      return renderEmpty(t("operational.noProjects", {}, "没有项目。"));
    }
    data.items.forEach((project) => content.appendChild(projectCard(project)));
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    renderTechnicalError(error);
  }
}

function projectCard(project) {
  const card = el("article", "card clickable project-card");
  card.append(
    el("h3", "card-title", project.name),
    el("p", "snippet", project.description),
    el("div", "row"),
    el(
      "p",
      "meta",
      t(
        "legacy.core.projects.cardMeta",
        {
          status: labelProjectStatus(project.status),
          priority: formatUiNumber(project.priority ?? 100),
          matches: formatUiNumber(project.match_count || 0),
          updated: project.latest_update_time_iso
            ? formatUiDateTime(project.latest_update_time_iso)
            : t("common.notRecorded", {}, "未记录"),
        },
        "状态：{status} · 优先级：{priority} · 匹配对话：{matches} · 最近更新：{updated}",
      ),
    ),
  );
  const sourceText = sourceCountsText(project.source_counts);
  if (sourceText) card.appendChild(el("p", "meta", sourceText));
  appendTags(card.querySelector(".row"), project.keywords.join(","));
  appendTags(card.querySelector(".row"), (project.aliases || []).join(","));
  makeKeyboardActivatable(
    card,
    () => loadProject(project.name, state.lastProjectSourcePlatform),
    t(
      "legacy.core.projects.openAria",
      { name: project.name },
      "打开项目：{name}",
    ),
  );
  return card;
}

async function loadProject(name, sourcePlatform = state.lastProjectSourcePlatform || "") {
  setActive("projects");
  const request = beginLegacyViewRequest("projects");
  setTitle(t("legacy.core.projects.namedTitle", { name }, "项目视图：{name}"));
  content.replaceChildren(el(
    "div",
    "panel",
    t("legacy.core.projects.loadingResults", {}, "正在加载项目结果…"),
  ));
  try {
    state.lastProjectSourcePlatform = sourcePlatform || "";
    const data = await api(
      `/api/project?name=${encodeURIComponent(name)}${sourceParam(state.lastProjectSourcePlatform)}`,
    );
    if (!isLegacyViewRequestCurrent(request)) return;
    renderProject(data);
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    renderTechnicalError(error);
  }
}

function renderProject(data) {
  content.innerHTML = "";
  appendReadonlyNotice();
  const project = data.project;
  state.lastProject = { name: project.name, data };
  const header = el("section", "panel");
  const actions = el("div", "actions");
  const backButton = button(
    t("legacy.core.action.backProjectView", {}, "返回项目视图"),
    "button secondary",
  );
  backButton.addEventListener("click", () => loadProjects(state.lastProjectSourcePlatform));
  const timelineButton = button(
    t("legacy.core.projects.viewTimeline", {}, "查看该项目时间轴"),
    "button secondary",
  );
  timelineButton.addEventListener("click", () =>
    loadTimeline({ project: project.name, groupBy: "month", sourcePlatform: state.lastProjectSourcePlatform }),
  );
  actions.append(backButton, timelineButton);
  header.append(
    el("h3", "card-title", project.name),
    el("p", "snippet", project.description),
    el(
      "p",
      "meta",
      t(
        "legacy.core.projects.detailMeta",
        {
          status: labelProjectStatus(project.status),
          priority: formatUiNumber(project.priority ?? 100),
          keywords: project.keywords.join("、"),
          aliases: (project.aliases || []).join("、")
            || t("common.notRecorded", {}, "未记录"),
          matches: formatUiNumber(project.match_count || 0),
          updated: project.latest_update_time_iso
            ? formatUiDateTime(project.latest_update_time_iso)
            : t("common.notRecorded", {}, "未记录"),
        },
        "状态：{status} · 优先级：{priority} · 关键词：{keywords} · 别名：{aliases} · 匹配对话：{matches} · 最近更新：{updated}",
      ),
    ),
    actions,
  );
  const sourceText = sourceCountsText(project.source_counts);
  if (sourceText) header.appendChild(el("p", "meta", sourceText));
  content.appendChild(header);

  const convPanel = el("section", "panel");
  convPanel.appendChild(el(
    "h3",
    "section-title",
    t("legacy.core.projects.relatedConversations", {}, "相关对话"),
  ));
  if (!data.matched_conversations.length) {
    convPanel.appendChild(el(
      "div",
      "empty-card",
      t(
        "legacy.core.projects.noRelatedConversations",
        {},
        "没有找到相关对话。",
      ),
    ));
  } else {
    data.matched_conversations.forEach((item) =>
      convPanel.appendChild(conversationCard(item, { fromProject: true, projectName: project.name })),
    );
  }
  content.appendChild(convPanel);

  const msgPanel = el("section", "panel");
  msgPanel.appendChild(el(
    "h3",
    "section-title",
    t("legacy.core.projects.relatedSnippets", {}, "相关片段"),
  ));
  if (!data.matched_messages.length) {
    msgPanel.appendChild(el(
      "div",
      "empty-card",
      t("legacy.core.projects.noRelatedSnippets", {}, "没有找到相关片段。"),
    ));
  } else {
    data.matched_messages.forEach((item) =>
      msgPanel.appendChild(
        searchResultCard(
          { ...item, source: "message" },
          "",
          { fromProject: true, projectName: project.name },
        ),
      ),
    );
  }
  content.appendChild(msgPanel);
}

function loadAbout() {
  setActive("about");
  setTitle(t("legacy.core.about.title", {}, "关于系统"));
  content.innerHTML = "";
  const panel = el("section", "panel");
  panel.append(
    el(
      "h3",
      "section-title",
      t(
        "legacy.core.about.heading",
        {},
        "YunOS 本地第二大脑原型",
      ),
    ),
    el(
      "p",
      "snippet",
      t(
        "legacy.core.about.database",
        {},
        "这个界面读取本机 SQLite 记忆库，默认使用测试库。标签和状态只写入本地 mark/tag 表。",
      ),
    ),
    el(
      "p",
      "snippet",
      t(
        "legacy.core.about.network",
        {},
        "当前不联网、不上传、不调用外部 API、不做 AI 总结、不接 Ollama，也不使用向量库。",
      ),
    ),
    el(
      "p",
      "snippet",
      t(
        "legacy.core.about.realDatabase",
        {},
        "真实记忆库必须通过 real 命令显式打开，并会显示清楚的写入警告。",
      ),
    ),
  );
  content.appendChild(panel);
}

function numberInput(id, value, min, max) {
  const input = el("input");
  input.id = id;
  input.type = "number";
  input.min = String(min);
  input.max = String(max);
  input.value = String(value);
  return input;
}

function checkboxInput(id, checked) {
  const input = el("input");
  input.id = id;
  input.type = "checkbox";
  input.checked = Boolean(checked);
  return input;
}

function metricCard(label, value) {
  const item = el("div", "health-metric");
  item.append(el("span", "health-metric-label", label), el("strong", "", String(value)));
  return item;
}

function runtimeBoolean(value) {
  return value === true
    ? t("common.yes", {}, "是")
    : value === false
      ? t("common.no", {}, "否")
      : t("common.unknown", {}, "未知");
}

async function loadRuntimeStatus() {
  setActive("runtimeStatus");
  const request = beginLegacyViewRequest("runtimeStatus");
  setTitle(t("runtime.productionStatus", {}, "生产 Runtime 状态"));
  content.innerHTML = "";
  appendReadonlyNotice();

  let data;
  try {
    data = await api("/api/runtime-sidecar-status");
  } catch (error) {
    if (!isLegacyViewRequestCurrent(request)) return;
    renderEmpty(t(
      "runtime.statusUnavailableDetail",
      { detail: error.message },
      "Runtime 状态不可用。技术详情：{detail}",
    ));
    return;
  }
  if (!isLegacyViewRequestCurrent(request)) return;

  const sidecar = data.sidecar || {};
  const identities = data.identities || {};
  const profile = data.runtime_profile || {};
  const counts = data.counts || {};
  const integrity = data.integrity || {};
  const execution = data.execution || {};

  const overview = el("section", "panel runtime-status-panel");
  const heading = el("div", "summary-health-block");
  heading.append(
    el("h3", "section-title", t("runtime.productionRuntime", {}, "D1 生产 Runtime")),
    el(
      "span",
      data.ready ? "health-pass" : "health-badge muted",
      enumLabel("status", data.status || "unknown"),
    ),
  );
  overview.append(
    heading,
    el(
      "p",
      "snippet runtime-sidecar-path",
      t(
        "runtime.fixedSidecarValue",
        { value: sidecar.path || t("common.unavailable", {}, "不可用") },
        "固定 sidecar：{value}",
      ),
    ),
  );
  const overviewMetrics = el("div", "summary-health-grid runtime-status-grid runtime-overview-grid");
  [
    [t("runtime.state", {}, "状态"), enumLabel("status", data.runtime_state || "unknown")],
    [t("runtime.initialized", {}, "已初始化"), runtimeBoolean(data.initialized)],
    [t("runtime.ready", {}, "已就绪"), runtimeBoolean(data.ready)],
    [t("runtime.readonlyInspection", {}, "只读检查"), runtimeBoolean(data.readonly)],
    [t("runtime.reason", {}, "原因"), data.reason_code || t("common.unknown", {}, "未知")],
    [
      t("runtime.initializedAt", {}, "初始化时间"),
      sidecar.initialized_at
        ? formatUiDateTime(sidecar.initialized_at)
        : t("common.unavailable", {}, "不可用"),
    ],
  ].forEach(([label, value]) => overviewMetrics.appendChild(metricCard(label, value)));
  overview.appendChild(overviewMetrics);
  content.appendChild(overview);

  const countPanel = el("section", "panel runtime-status-panel");
  countPanel.appendChild(
    el("h3", "section-title", t("runtime.tableCounts", {}, "Runtime 表记录数")),
  );
  const countMetrics = el("div", "summary-health-grid runtime-status-grid runtime-count-grid");
  [
    [t("runtime.conversations", {}, "会话"), counts.conversations],
    [t("runtime.requestArtifacts", {}, "请求产物"), counts.request_artifacts],
    [t("runtime.modelRuns", {}, "模型运行"), counts.model_runs],
    [t("runtime.messages", {}, "消息"), counts.messages],
    [t("runtime.events", {}, "事件"), counts.events],
    [t("runtime.errors", {}, "错误"), counts.errors],
    [t("runtime.receipts", {}, "回执"), counts.receipts],
  ].forEach(([label, value]) => countMetrics.appendChild(
    metricCard(label, value === null || value === undefined
      ? t("common.unavailable", {}, "不可用")
      : formatUiNumber(value)),
  ));
  countPanel.appendChild(countMetrics);
  content.appendChild(countPanel);

  const configuration = el("section", "panel runtime-status-panel");
  configuration.appendChild(el(
    "h3",
    "section-title",
    t("runtime.fixedProviderConfiguration", {}, "固定 Provider 配置"),
  ));
  const configMetrics = el("div", "summary-health-grid runtime-status-grid");
  [
    [t("runtime.provider", {}, "Provider"), profile.provider === "deepseek" ? "DeepSeek" : (profile.provider || t("common.unavailable", {}, "不可用"))],
    [t("runtime.model", {}, "模型"), profile.model || t("common.unavailable", {}, "不可用")],
    [t("runtime.baseUrl", {}, "Base URL"), profile.base_url || t("common.unavailable", {}, "不可用")],
    [t("runtime.stream", {}, "流式输出"), runtimeBoolean(profile.stream)],
    [t("runtime.thinkingType", {}, "思考类型"), profile.thinking?.type || t("common.unavailable", {}, "不可用")],
  ].forEach(([label, value]) => configMetrics.appendChild(metricCard(label, value)));
  configuration.appendChild(configMetrics);
  content.appendChild(configuration);

  const identityPanel = el("section", "panel runtime-status-panel");
  identityPanel.appendChild(
    el("h3", "section-title", t("runtime.versionedIdentities", {}, "版本化标识")),
  );
  const identityMetrics = el("div", "summary-health-grid runtime-status-grid");
  [
    [t("runtime.productionProfile", {}, "生产 Profile"), identities.production_profile],
    [t("runtime.protocol", {}, "Runtime 协议"), identities.runtime_protocol],
    [t("runtime.schema", {}, "Runtime schema"), sidecar.schema_version || identities.runtime_schema],
    [t("runtime.artifactProtocol", {}, "产物协议"), identities.artifact_protocol],
    [t("runtime.wireRenderer", {}, "Wire renderer"), identities.wire_renderer],
  ].forEach(([label, value]) => identityMetrics.appendChild(
    metricCard(label, value || t("common.unavailable", {}, "不可用")),
  ));
  identityPanel.appendChild(identityMetrics);
  content.appendChild(identityPanel);

  const assurancePanel = el("section", "panel runtime-status-panel");
  assurancePanel.appendChild(el(
    "h3",
    "section-title",
    t("runtime.integrityAssurance", {}, "完整性与执行保证"),
  ));
  const assuranceMetrics = el("div", "summary-health-grid runtime-status-grid");
  [
    [t("runtime.integrityCheck", {}, "完整性检查"), integrity.integrity_check || t("common.unavailable", {}, "不可用")],
    [t("runtime.quickCheck", {}, "快速检查"), integrity.quick_check || t("common.unavailable", {}, "不可用")],
    [t("runtime.foreignKeyViolations", {}, "外键违规"), integrity.foreign_key_violations ?? t("common.unavailable", {}, "不可用")],
    [t("runtime.appendOnly", {}, "仅追加"), runtimeBoolean(integrity.append_only)],
    [t("runtime.appendOnlyTriggers", {}, "仅追加触发器"), integrity.trigger_count ?? t("common.unavailable", {}, "不可用")],
    [t("runtime.credentialRead", {}, "读取凭据"), runtimeBoolean(execution.credential_read)],
    [t("runtime.sentToModel", {}, "已发送至模型"), runtimeBoolean(execution.sent_to_model)],
    [t("runtime.externalModelCalls", {}, "外部模型调用"), runtimeBoolean(execution.external_model_calls)],
  ].forEach(([label, value]) => assuranceMetrics.appendChild(metricCard(label, value)));
  assurancePanel.appendChild(assuranceMetrics);
  content.appendChild(assurancePanel);
}

function runtimeIdentifier(prefix) {
  const random = globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}

function normalizeRuntimeQuestion(value) {
  return String(value || "").trim().replace(/\s+/g, " ");
}

function loadRuntimeExecution() {
  setActive("runtimeExecution");
  setTitle(state.runtimeReview
    ? t("runtime.reviewOnlyPreview", {}, "Runtime 只读复核预览")
    : t("runtime.execution", {}, "生产 Runtime 执行"));
  content.innerHTML = "";

  const endpointPanel = el("section", "panel runtime-service-endpoint");
  endpointPanel.append(
    el("h3", "section-title", t("runtime.boundEndpoint", {}, "绑定的服务端点")),
    el(
      "p",
      "snippet",
      state.boundEndpoint || t("runtime.endpointUnavailable", {}, "端点不可用"),
    ),
  );
  const endpointMetrics = el("div", "summary-health-grid runtime-status-grid");
  [
    [t("legacy.tools.runtime.serviceMode", {}, "服务模式"), state.serviceMode],
    [t("legacy.tools.runtime.boundHost", {}, "绑定主机"), state.boundHost || t("common.unavailable", {}, "不可用")],
    [t("legacy.tools.runtime.boundPort", {}, "绑定端口"), state.boundPort || t("common.unavailable", {}, "不可用")],
    [t("legacy.tools.runtime.processId", {}, "进程 ID"), state.processId ?? t("common.unavailable", {}, "不可用")],
    [t("legacy.tools.runtime.serverInstanceId", {}, "服务实例 ID"), state.serverInstanceId || t("common.unavailable", {}, "不可用")],
    [t("legacy.tools.runtime.adminMode", {}, "Runtime 管理模式"), runtimeBoolean(state.runtimeAdmin)],
    [t("legacy.tools.runtime.reviewMode", {}, "Runtime 复核模式"), runtimeBoolean(state.runtimeReview)],
    [t("legacy.tools.common.readonly", {}, "只读"), runtimeBoolean(state.serviceReadonly)],
    [t("legacy.tools.runtime.executionAuthority", {}, "执行授权"), runtimeBoolean(state.executionAuthorityPresent)],
    [t("legacy.tools.runtime.canExecute", {}, "允许执行"), runtimeBoolean(state.canExecute)],
  ].forEach(([label, value]) => endpointMetrics.appendChild(metricCard(label, value)));
  endpointPanel.appendChild(endpointMetrics);
  content.appendChild(endpointPanel);

  const saved = state.lastRuntimeExecution;
  const panel = el("section", "panel runtime-execution-controls");
  panel.append(
    el("h3", "section-title", state.runtimeReview
      ? t("runtime.reviewOnlyRequest", {}, "Runtime 只读复核请求")
      : t("runtime.reviewedRequest", {}, "已复核的 D1 请求")),
    el("p", "snippet", state.runtimeReview
      ? t(
        "runtime.reviewOnlyDescription",
        {},
        "使用调用方指定的 ID 构建固定生产 Delivery 和不可变复核对象。此过程没有执行授权，也没有发送路径。",
      )
      : (state.runtimeAdmin
        ? t(
          "runtime.adminPreviewDescription",
          {},
          "预览固定的 v8.0-E Delivery 和不可变 D1 请求产物。明确确认所示标识前，发送保持禁用。",
        )
        : t(
          "runtime.readonlyPreviewDescription",
          {},
          "当前为只读预览模式。real-readonly 服务不能发送、读取凭据或追加 Runtime 账本。",
        ))),
  );
  if (state.runtimeReview) {
    const reviewBanner = el("section", "runtime-review-banner");
    reviewBanner.append(
      el("strong", "", t("runtime.reviewOnly", {}, "仅限 Runtime 复核")),
      el(
        "p",
        "",
        t(
          "runtime.executionAuthorityAbsent",
          {},
          "当前没有执行授权；预览构建为只读操作，无法发送。",
        ),
      ),
    );
    const reviewMetrics = el("div", "summary-health-grid runtime-status-grid");
    [
      [t("legacy.tools.runtime.reviewMode", {}, "Runtime 复核模式"), runtimeBoolean(state.runtimeReview)],
      [t("legacy.tools.runtime.adminMode", {}, "Runtime 管理模式"), runtimeBoolean(state.runtimeAdmin)],
      [t("legacy.tools.common.readonly", {}, "只读"), runtimeBoolean(state.serviceReadonly)],
      [t("legacy.tools.runtime.executionAuthority", {}, "执行授权"), runtimeBoolean(state.executionAuthorityPresent)],
      [t("legacy.tools.runtime.canExecute", {}, "允许执行"), runtimeBoolean(state.canExecute)],
    ].forEach(([label, value]) => reviewMetrics.appendChild(metricCard(label, value)));
    reviewBanner.appendChild(reviewMetrics);
    panel.appendChild(reviewBanner);
  }
  if (state.runtimeMock) {
    panel.appendChild(el(
      "p",
      "runtime-mock-banner",
      t(
        "runtime.visibleAcceptanceDescription",
        {},
        "可见验收模式 — 临时 Runtime sidecar、模拟传输；credential_read=false，network_request_count=0。",
      ),
    ));
  }

  const questionLabel = el("label", "control-label prompt-input-label");
  const question = el("textarea");
  question.id = "runtime-execution-question";
  question.placeholder = t(
    "runtime.questionPlaceholder",
    {},
    "输入要复核并绑定的确切问题。",
  );
  question.value = saved.question || "";
  questionLabel.append(el("span", "", t("runtime.exactQuestion", {}, "确切问题")), question);

  const controls = el("div", "context-review-grid");
  const budgetLabel = el("label", "control-label");
  const budget = el("select");
  budget.id = "runtime-execution-budget";
  [
    ["Small", t("budget.small", {}, "小")],
    ["Medium", t("budget.medium", {}, "中")],
    ["Large", t("budget.large", {}, "大")],
  ].forEach(([name, label]) => {
    const option = el("option", "", label);
    option.value = name;
    budget.appendChild(option);
  });
  budget.value = saved.budget || "Medium";
  budgetLabel.append(el("span", "", t("runtime.budgetProfile", {}, "预算档位")), budget);
  controls.appendChild(budgetLabel);

  const requestLabel = el("label", "control-label");
  const requestId = el(state.runtimeReview ? "textarea" : "input");
  requestId.id = "runtime-execution-request-id";
  if (!state.runtimeReview) requestId.type = "text";
  if (state.runtimeReview) {
    requestId.rows = 2;
    requestId.className = "runtime-review-id-field";
  }
  requestId.autocomplete = "off";
  requestId.spellcheck = false;
  requestId.placeholder = t("runtime.exactRequestId", {}, "确切 request_id");
  requestId.value = saved.requestId || "";
  requestLabel.append(
    el("span", "", t("legacy.tools.runtime.requestIdField", {}, "request_id")),
    requestId,
  );
  if (state.runtimeReview) {
    const requestIdEcho = el(
      "output",
      "runtime-review-id-echo",
      requestId.value || t("runtime.requestIdEcho", {}, "确切 request_id 将显示在这里。"),
    );
    requestId.addEventListener("input", () => {
      requestIdEcho.textContent = requestId.value
        || t("runtime.requestIdEcho", {}, "确切 request_id 将显示在这里。");
    });
    requestLabel.appendChild(requestIdEcho);
  }
  controls.appendChild(requestLabel);

  const mutationLabel = el("label", "control-label");
  const mutationId = el(state.runtimeReview ? "textarea" : "input");
  mutationId.id = "runtime-execution-mutation-id";
  if (!state.runtimeReview) mutationId.type = "text";
  if (state.runtimeReview) {
    mutationId.rows = 2;
    mutationId.className = "runtime-review-id-field";
  }
  mutationId.autocomplete = "off";
  mutationId.spellcheck = false;
  mutationId.placeholder = t("runtime.exactMutationId", {}, "确切 mutation_id");
  mutationId.value = saved.mutationId || "";
  mutationLabel.append(
    el("span", "", t("legacy.tools.runtime.mutationIdField", {}, "mutation_id")),
    mutationId,
  );
  if (state.runtimeReview) {
    const mutationIdEcho = el(
      "output",
      "runtime-review-id-echo",
      mutationId.value || t("runtime.mutationIdEcho", {}, "确切 mutation_id 将显示在这里。"),
    );
    mutationId.addEventListener("input", () => {
      mutationIdEcho.textContent = mutationId.value
        || t("runtime.mutationIdEcho", {}, "确切 mutation_id 将显示在这里。");
    });
    mutationLabel.appendChild(mutationIdEcho);
  }
  controls.appendChild(mutationLabel);

  const actions = el("div", "actions");
  const previewButton = button(t("runtime.buildPreview", {}, "构建复核预览"));
  previewButton.id = "runtime-execution-preview";
  actions.appendChild(previewButton);
  const message = el(
    "p",
    "form-message",
    t(
      "legacy.tools.runtime.safePreviewFlags",
      {},
      "persisted=false · sent_to_model=false · external_model_calls=false · credential_read=false",
    ),
  );
  message.id = "runtime-execution-message";
  panel.append(questionLabel, controls, actions, message);

  const results = el("section", "content runtime-execution-results");
  results.id = "runtime-execution-results";
  content.append(panel, results);

  const invalidate = () => {
    if (!state.lastRuntimeExecution.preview) return;
    state.lastRuntimeExecution = {
      question: question.value,
      budget: budget.value,
      requestId: requestId.value,
      mutationId: mutationId.value,
      preview: null,
      result: null,
    };
    renderEmpty(t("runtime.previewRequired", {}, "输入已变化。确认前请重新构建复核预览。"), results);
    setMessage(
      message,
      t("runtime.previewInvalidated", {}, "输入变化已使预览失效；没有发送或持久化任何内容。"),
    );
  };
  question.addEventListener("input", invalidate);
  budget.addEventListener("change", invalidate);
  requestId.addEventListener("input", invalidate);
  mutationId.addEventListener("input", invalidate);
  previewButton.addEventListener("click", runRuntimeExecutionPreview);

  if (saved.preview) {
    renderRuntimeExecutionPreview(saved.preview, saved.result);
  } else {
    renderEmpty(t("runtime.noPreview", {}, "尚未构建 D1 执行预览。"), results);
  }
  question.focus();
}

async function runRuntimeExecutionPreview() {
  const questionInput = document.querySelector("#runtime-execution-question");
  const budgetInput = document.querySelector("#runtime-execution-budget");
  const requestInput = document.querySelector("#runtime-execution-request-id");
  const mutationInput = document.querySelector("#runtime-execution-mutation-id");
  const message = document.querySelector("#runtime-execution-message");
  const results = document.querySelector("#runtime-execution-results");
  const question = state.runtimeReview
    ? String(questionInput?.value || "")
    : normalizeRuntimeQuestion(questionInput?.value);
  const budget = budgetInput?.value || "Medium";
  if (!question) {
    setMessage(message, t("runtime.questionRequired", {}, "构建预览前请输入问题。"), "error");
    renderEmpty(t("runtime.questionRequiredShort", {}, "必须输入问题。"), results);
    return;
  }
  if (!state.runtimeReview) questionInput.value = question;
  const callerRequestId = String(requestInput?.value || "");
  const callerMutationId = String(mutationInput?.value || "");
  if (state.runtimeReview && (!callerRequestId || !callerMutationId)) {
    setMessage(
      message,
      t("runtime.idsRequired", {}, "构建复核前请输入确切的 request_id 和 mutation_id。"),
      "error",
    );
    renderEmpty(
      t(
        "runtime.idsRequiredShort",
        {},
        "只读复核模式必须提供确切的 request_id 和 mutation_id。",
      ),
      results,
    );
    return;
  }
  const requestId = callerRequestId || runtimeIdentifier("request");
  const mutationId = callerMutationId || runtimeIdentifier("mutation");
  const params = new URLSearchParams({
    q: question,
    budget,
    request_id: requestId,
    mutation_id: mutationId,
  });
  setMessage(
    message,
    t(
      "runtime.buildingArtifact",
      {},
      "正在构建固定 Delivery 和 D1 产物；此过程不读取凭据，也不使用传输。",
    ),
  );
  results.replaceChildren(el(
    "div",
    "panel",
    t(
      "runtime.buildingPreview",
      {},
      "正在构建 Runtime 复核预览…",
    ),
  ));
  try {
    const preview = await api(`/api/runtime-preview?${params.toString()}`);
    state.lastRuntimeExecution = {
      question,
      budget,
      requestId,
      mutationId,
      preview,
      result: null,
    };
    if (requestInput) requestInput.value = preview.request_id;
    if (mutationInput) mutationInput.value = preview.mutation_id;
    renderRuntimeExecutionPreview(preview, null);
    setMessage(
      message,
      state.runtimeReview
        ? t(
          "runtime.reviewCompleteDetail",
          {},
          "复核完成。调用方指定的 ID 已冻结；当前没有执行授权。",
        )
        : state.runtimeAdmin
        ? t(
          "runtime.adminPreviewComplete",
          {},
          "预览已冻结。请核对每个字段，再使用明确确认门禁。",
        )
        : t(
          "runtime.readonlyPreviewComplete",
          {},
          "预览完成。real-readonly 模式没有发送能力。",
        ),
      "success",
    );
  } catch (error) {
    state.lastRuntimeExecution = {
      question,
      budget,
      requestId: callerRequestId,
      mutationId: callerMutationId,
      preview: null,
      result: null,
    };
    renderTechnicalError(
      error,
      results,
      "error.runtimePreviewUnavailable",
      "无法连接本机 Runtime 预览服务。",
    );
    setTechnicalMessage(
      message,
      error,
      "error.runtimePreviewUnavailable",
      "无法连接本机 Runtime 预览服务。",
    );
  }
}

function runtimeIdentityPanel(preview) {
  const artifact = preview.request_artifact || {};
  const binding = preview.reviewed_execution_binding || {};
  const reviewed = binding.reviewed_artifact || {};
  const diagnostics = preview.diagnostic_bindings || {};
  const unavailable = t("common.unavailable", {}, "不可用");
  const none = t("legacy.tools.common.none", {}, "无");
  const panel = el("section", "panel runtime-execution-identities");
  panel.appendChild(el(
    "h3",
    "section-title",
    t("runtime.reviewedExecutionBinding", {}, "已复核执行绑定"),
  ));
  panel.appendChild(el(
    "pre",
    "runtime-identity-proof",
    [
      t(
        "legacy.tools.runtime.proof.reviewedBinding",
        { value: binding.identity || unavailable },
        "复核绑定：{value}",
      ),
      t(
        "legacy.tools.runtime.proof.reviewedArtifact",
        { value: reviewed.identity || unavailable },
        "复核产物：{value}",
      ),
      t(
        "legacy.tools.runtime.proof.modelContextSha256",
        { value: reviewed.model_context?.sha256 || unavailable },
        "模型上下文 SHA-256：{value}",
      ),
      t(
        "legacy.tools.runtime.proof.wireBodySha256",
        { value: reviewed.wire?.sha256 || unavailable },
        "传输正文 SHA-256：{value}",
      ),
      t(
        "legacy.tools.runtime.proof.deliveryDiagnostic",
        { value: diagnostics.delivery_identity || unavailable },
        "交付诊断标识：{value}",
      ),
      t(
        "legacy.tools.runtime.proof.assemblyDiagnostic",
        { value: diagnostics.assembly_identity || unavailable },
        "装配诊断标识：{value}",
      ),
      t(
        "legacy.tools.runtime.proof.requestArtifact",
        { value: diagnostics.artifact_identity || artifact.artifact_identity || unavailable },
        "D1 请求产物：{value}",
      ),
    ].join("\n"),
  ));
  const metrics = el("div", "summary-health-grid runtime-status-grid");
  [
    [t("runtime.reviewedBindingVersion", {}, "复核绑定版本"), binding.version],
    [t("runtime.reviewedBindingIdentity", {}, "复核绑定标识"), binding.identity],
    [t("runtime.reviewedArtifactVersion", {}, "复核产物版本"), reviewed.version],
    [t("runtime.reviewedArtifactIdentity", {}, "复核产物标识"), reviewed.identity],
    [t("runtime.idempotencyFingerprint", {}, "幂等指纹"), binding.idempotency_fingerprint],
    [t("runtime.requestId", {}, "Request ID"), binding.request_id],
    [t("runtime.mutationId", {}, "Mutation ID"), binding.mutation_id],
    [t("runtime.questionSha256", {}, "问题 UTF-8 SHA-256"), reviewed.question?.utf8_sha256],
    [t("runtime.budgetBinding", {}, "预算绑定"), `${reviewed.budget?.profile || unavailable} / ${reviewed.budget?.version || unavailable}`],
    [t("runtime.projectionIdentity", {}, "投影标识"), reviewed.approved_profile?.projection_identity],
    [t("runtime.profileRevision", {}, "画像修订版"), reviewed.approved_profile?.revision],
    [t("runtime.orderedProfileIds", {}, "有序画像 ID"), (reviewed.approved_profile?.profile_item_ids || []).join(", ") || none],
    [t("runtime.orderedEvidenceIds", {}, "有序证据 ID"), (reviewed.evidence || []).map((item) => item.evidence_reference_id).join(", ") || none],
    [t("runtime.modelContextSha256", {}, "模型上下文 SHA-256"), reviewed.model_context?.sha256],
    [t("runtime.wireBodySha256", {}, "Wire body SHA-256"), reviewed.wire?.sha256],
    [t("runtime.artifactProtocol", {}, "产物协议"), reviewed.artifact_protocol],
    [t("runtime.renderer", {}, "Renderer"), reviewed.renderer?.version],
  ].forEach(([label, value]) => metrics.appendChild(
    metricCard(label, value || t("common.unavailable", {}, "不可用")),
  ));
  panel.appendChild(metrics);
  panel.appendChild(el(
    "h4",
    "section-title",
    t("runtime.diagnosticIdentities", {}, "诊断标识（不代表发送授权）"),
  ));
  const diagnosticMetrics = el("div", "summary-health-grid runtime-status-grid");
  [
    [t("runtime.diagnosticsVersion", {}, "诊断版本"), diagnostics.version],
    [t("runtime.diagnosticsIdentity", {}, "诊断标识"), diagnostics.identity],
    [t("runtime.deliveryDiagnosticIdentity", {}, "交付诊断标识"), diagnostics.delivery_identity],
    [t("runtime.assemblyDiagnosticIdentity", {}, "装配诊断标识"), diagnostics.assembly_identity],
    [t("runtime.requestArtifactIdentity", {}, "D1 请求产物标识"), diagnostics.artifact_identity || artifact.artifact_identity],
    [t("runtime.requestFingerprint", {}, "D1 请求指纹"), diagnostics.request_fingerprint],
    [t("runtime.modelRunIdentity", {}, "D1 模型运行标识"), diagnostics.model_run_id],
    [t("runtime.conversationIdentity", {}, "D1 会话标识"), diagnostics.conversation_id],
  ].forEach(([label, value]) => diagnosticMetrics.appendChild(
    metricCard(label, value || t("common.unavailable", {}, "不可用")),
  ));
  panel.appendChild(diagnosticMetrics);
  const bindingDetails = el("details", "runtime-reviewed-object");
  bindingDetails.open = true;
  bindingDetails.append(
    el(
      "summary",
      "context-layer-summary",
      t("runtime.completeReviewedObject", {}, "完整的不可变复核执行对象"),
    ),
    el("pre", "context-json-preview", JSON.stringify(binding, null, 2)),
  );
  panel.appendChild(bindingDetails);
  const artifactDetails = el("details", "runtime-reviewed-object");
  artifactDetails.append(
    el(
      "summary",
      "context-layer-summary",
      t("runtime.publicRequestArtifact", {}, "公开 D1 请求产物"),
    ),
    el("pre", "context-json-preview", JSON.stringify(artifact, null, 2)),
  );
  panel.appendChild(artifactDetails);
  return panel;
}

function runtimeLayerPanel(titleText, items, formatter) {
  const details = el("details", "panel context-layer");
  details.open = true;
  details.appendChild(el(
    "summary",
    "context-layer-summary",
    t(
      "legacy.tools.runtime.layerSummary",
      { title: titleText, count: formatUiNumber(items.length) },
      "{title} · {count} 项",
    ),
  ));
  if (!items.length) {
    details.appendChild(el("div", "empty-card", t("context.noItems", {}, "没有项目。")));
  }
  items.forEach((item) => details.appendChild(el("pre", "context-json-preview", formatter(item))));
  return details;
}

function renderRuntimeExecutionPreview(preview, executionEnvelope = null) {
  const results = document.querySelector("#runtime-execution-results");
  if (!results) return;
  results.innerHTML = "";
  const delivery = preview.delivery || {};
  const artifact = preview.request_artifact || {};
  const profile = artifact.runtime_profile || preview.runtime_profile || {};
  const profileItems = delivery.layers?.approved_profile?.items || [];
  const evidenceItems = delivery.layers?.historical_evidence?.items || [];

  const overview = el("section", "panel runtime-execution-overview");
  overview.append(
    el("h3", "section-title", t("runtime.reviewedProductionRequest", {}, "已复核的生产请求")),
    el("p", "runtime-exact-question", preview.question || ""),
  );
  const metrics = el("div", "summary-health-grid runtime-status-grid");
  [
    [t("runtime.protocolLabel", {}, "协议"), preview.protocol_version],
    [t("runtime.provider", {}, "Provider"), profile.provider === "deepseek" ? "DeepSeek" : profile.provider],
    [t("runtime.model", {}, "模型"), profile.model === "deepseek-v4-flash" ? "DeepSeek v4 Flash (deepseek-v4-flash)" : profile.model],
    [t("runtime.baseUrl", {}, "Base URL"), profile.base_url],
    [t("runtime.stream", {}, "流式输出"), runtimeBoolean(profile.stream)],
    [t("runtime.thinkingType", {}, "思考类型"), profile.thinking?.type],
    [t("runtime.budgetProfile", {}, "预算档位"), preview.budget_profile],
    [t("context.approvedProfile", {}, "已批准画像"), `${preview.review?.approved_profile_included ?? 0}/${preview.review?.approved_profile_available ?? 0}`],
    [t("runtime.evidenceAnchors", {}, "证据锚点"), formatUiNumber(evidenceItems.length)],
    [t("runtime.estimatedInputTokens", {}, "预计输入 Token"), preview.review?.estimated_tokens ?? t("common.unavailable", {}, "不可用")],
    [t("runtime.persisted", {}, "已持久化"), runtimeBoolean(preview.execution?.persisted)],
    [t("runtime.credentialRead", {}, "读取凭据"), runtimeBoolean(preview.execution?.credential_read)],
    [t("runtime.sentToModel", {}, "已发送至模型"), runtimeBoolean(preview.execution?.sent_to_model)],
    [t("runtime.externalModelCalls", {}, "外部模型调用"), runtimeBoolean(preview.execution?.external_model_calls)],
  ].forEach(([label, value]) => metrics.appendChild(
    metricCard(label, value ?? t("common.unavailable", {}, "不可用")),
  ));
  overview.appendChild(metrics);
  results.append(overview, runtimeIdentityPanel(preview));

  results.appendChild(runtimeLayerPanel(
    t("runtime.approvedProfileItems", {}, "已批准画像项"),
    profileItems,
    (item) => JSON.stringify({
      profile_item_id: item.profile_item_id,
      statement: item.statement,
      model_visible: item.model_visible,
      source: item.source,
    }, null, 2),
  ));
  results.appendChild(runtimeLayerPanel(
    t("runtime.historicalEvidenceAnchors", {}, "历史证据锚点"),
    evidenceItems,
    (item) => JSON.stringify({
      evidence_reference_id: item.evidence_reference_id,
      source_anchor: item.source_anchor,
      excerpt: item.excerpt,
    }, null, 2),
  ));
  results.appendChild(runtimeLayerPanel(
    t("runtime.uncertaintySection", {}, "不确定性"),
    delivery.uncertainties || [],
    (item) => JSON.stringify(item, null, 2),
  ));

  const contextPanel = el("section", "panel final-model-context");
  contextPanel.append(
    el("h3", "section-title", t("runtime.completeModelContext", {}, "完整模型上下文")),
    el("pre", "model-context-preview runtime-model-context", delivery.model_context || ""),
  );
  results.appendChild(contextPanel);

  if (executionEnvelope) {
    renderRuntimeExecutionResult(executionEnvelope, results);
    return;
  }

  const confirmation = el("section", "panel runtime-confirmation-panel");
  confirmation.appendChild(el(
    "h3",
    "section-title",
    t("runtime.explicitConfirmation", {}, "明确发送确认"),
  ));
  const label = el("label", "runtime-confirmation-label");
  const checkbox = checkboxInput("runtime-execution-confirm", false);
  const sidecarReady = preview.runtime_sidecar?.ready === true;
  if (state.runtimeReview) {
    checkbox.checked = false;
    checkbox.disabled = true;
    label.append(
      checkbox,
      el(
        "span",
        "",
        t("runtime.confirmationDisabled", {}, "只读复核模式下确认框保持未选中并已禁用。"),
      ),
    );
    const send = button(t("common.send", {}, "发送"));
    send.id = "runtime-execution-send";
    send.disabled = true;
    const actions = el("div", "actions", "");
    actions.appendChild(send);
    confirmation.append(
      label,
      actions,
      el(
        "p",
        "form-message runtime-review-explanation",
        t(
          "runtime.sendUnavailable",
          {},
          "只读复核模式：当前没有执行授权，无法发送。",
        ),
      ),
    );
    results.appendChild(confirmation);
    return;
  }
  if (!state.runtimeAdmin) {
    confirmation.appendChild(el(
      "p",
      "snippet",
      t(
        "runtime.adminRequired",
        {},
        "web-real-readonly 中不可用。只有显式启动 Runtime-admin 服务后才会显示确认和发送控件。",
      ),
    ));
    results.appendChild(confirmation);
    return;
  }
  checkbox.disabled = !sidecarReady;
  label.append(
    checkbox,
    el(
      "span",
      "",
      t(
        "runtime.confirmationStatement",
        {},
        "我已核对确切问题、复核执行绑定、哈希、固定 Provider 配置、request_id 和 mutation_id。诊断标识不代表发送授权。",
      ),
    ),
  );
  const send = button(state.runtimeMock
    ? t("runtime.executeMockConfirmed", {}, "执行已确认的模拟回合")
    : t("runtime.executeDeepSeekConfirmed", {}, "执行已确认的 DeepSeek 回合"));
  send.id = "runtime-execution-send";
  send.disabled = true;
  checkbox.addEventListener("change", () => { send.disabled = !sidecarReady || !checkbox.checked; });
  send.addEventListener("click", executeReviewedRuntimeRequest);
  confirmation.append(label, el("div", "actions", ""));
  if (!sidecarReady) {
    confirmation.appendChild(el(
      "p",
      "form-message error",
      t("runtime.sidecarNotReady", {}, "Runtime sidecar 尚未就绪；执行保持禁用。"),
    ));
  }
  confirmation.querySelector(".actions").appendChild(send);
  results.appendChild(confirmation);
}

async function executeReviewedRuntimeRequest() {
  const saved = state.lastRuntimeExecution;
  const checkbox = document.querySelector("#runtime-execution-confirm");
  const send = document.querySelector("#runtime-execution-send");
  const message = document.querySelector("#runtime-execution-message");
  if (
    state.runtimeReview
    || !state.runtimeAdmin
    || !state.canExecute
    || !saved.preview
    || !checkbox?.checked
    || !send
  ) return;
  send.disabled = true;
  checkbox.disabled = true;
  setMessage(
    message,
    t("runtime.executingOnce", {}, "正在执行一次确切的已确认绑定；自动重试已禁用。"),
  );
  try {
    const envelope = await api("/api/runtime-execute", {
      method: "POST",
      body: JSON.stringify({
        q: saved.preview.question,
        budget: saved.preview.budget_profile,
        request_id: saved.preview.request_id,
        mutation_id: saved.preview.mutation_id,
        expected_bindings: saved.preview.reviewed_execution_binding,
        confirmed: true,
      }),
    });
    state.lastRuntimeExecution.result = envelope;
    renderRuntimeExecutionPreview(saved.preview, envelope);
    setMessage(
      message,
      t(
        "runtime.executionResultDetail",
        {
          status: enumLabel("status", envelope.status),
          phase: envelope.execution_phase,
        },
        "执行结果：{status}（{phase}）。",
      ),
      envelope.status === "succeeded" ? "success" : "error",
    );
  } catch (error) {
    setTechnicalMessage(
      message,
      error,
      "error.executionResultUnconfirmedSummary",
      "无法安全确认执行结果。系统不会自动重试。",
    );
    send.disabled = false;
    checkbox.disabled = false;
  }
}

function renderRuntimeExecutionResult(envelope, target) {
  const result = envelope.result || {};
  const transport = envelope.transport || {};
  const panel = el("section", "panel runtime-execution-result");
  panel.appendChild(el(
    "h3",
    "section-title",
    t(
      "runtime.executionResultStatus",
      { status: enumLabel("status", result.status || "unknown") },
      "执行结果：{status}",
    ),
  ));
  const metrics = el("div", "summary-health-grid runtime-status-grid");
  [
    [t("runtime.executionPhase", {}, "执行阶段"), result.execution_phase],
    [t("runtime.promptTokens", {}, "Prompt Token"), formatUiNumber(result.usage?.prompt_tokens ?? 0)],
    [t("runtime.completionTokens", {}, "完成 Token"), formatUiNumber(result.usage?.completion_tokens ?? 0)],
    [t("runtime.totalTokens", {}, "Token 总数"), formatUiNumber(result.usage?.total_tokens ?? 0)],
    [t("runtime.adapterConstructorCount", {}, "Adapter 构造次数"), formatUiNumber(transport.adapter_constructor_count_this_request ?? 0)],
    [t("runtime.modelCallsForRun", {}, "本次运行模型调用"), formatUiNumber(transport.model_call_count_for_run ?? 0)],
    [t("runtime.exactlyOneCall", {}, "恰好一次调用"), runtimeBoolean(transport.exactly_one_call)],
    [t("runtime.automaticRetry", {}, "自动重试"), runtimeBoolean(transport.automatic_retry)],
    [t("runtime.networkRequestCount", {}, "网络请求数"), transport.network_request_count ?? t("common.unavailable", {}, "不可用")],
    [t("runtime.credentialRead", {}, "读取凭据"), runtimeBoolean(transport.credential_read)],
    [t("runtime.mockTransport", {}, "模拟传输"), runtimeBoolean(transport.mock_transport)],
    [t("runtime.finishReason", {}, "结束原因"), result.response?.finish_reason || t("common.unavailable", {}, "不可用")],
  ].forEach(([label, value]) => metrics.appendChild(metricCard(label, value)));
  panel.appendChild(metrics);
  if (result.response?.content) {
    panel.append(
      el("h4", "section-title", t("runtime.modelResponse", {}, "模型回答")),
      el("pre", "model-context-preview runtime-response", result.response.content),
    );
  }
  if (result.error) {
    panel.appendChild(el("pre", "context-json-preview", JSON.stringify(result.error, null, 2)));
  }
  target.appendChild(panel);
}

function loadContextReview() {
  setActive("contextReview");
  setTitle(t("context.preview", {}, "上下文预览"));
  content.innerHTML = "";
  appendReadonlyNotice();

  const saved = state.lastContextReview;
  const panel = el("section", "panel context-review-controls");
  panel.append(
    el("h3", "section-title", t("context.currentModelContext", {}, "本次模型上下文")),
    el(
      "p",
      "snippet",
      t(
        "context.readonlyDescription",
        {},
        "只读预览。临时排除仅作用于当前 Delivery，不修改画像、候选或历史数据，也不会调用模型。",
      ),
    ),
  );
  const queryLabel = el("label", "control-label prompt-input-label");
  queryLabel.appendChild(el("span", "", t("context.currentRequest", {}, "查询 / 当前请求")));
  const query = el("textarea");
  query.id = "context-review-query";
  query.placeholder = t(
    "context.queryPlaceholder",
    {},
    "输入本次请求，装配并审查模型将看到的上下文",
  );
  query.value = saved.query || "";
  queryLabel.appendChild(query);

  const controls = el("div", "context-review-grid");
  const startLabel = el("label", "control-label");
  const start = el("input");
  start.id = "context-review-start";
  start.type = "date";
  start.value = saved.startTime || "";
  startLabel.append(el("span", "", t("date.startTime", {}, "开始时间")), start);
  const endLabel = el("label", "control-label");
  const end = el("input");
  end.id = "context-review-end";
  end.type = "date";
  end.value = saved.endTime || "";
  endLabel.append(el("span", "", t("date.endTime", {}, "结束时间")), end);
  const budgetLabel = el("label", "control-label");
  const budget = el("select");
  budget.id = "context-review-budget";
  [
    ["Small", t("budget.small", {}, "小")],
    ["Medium", t("budget.medium", {}, "中")],
    ["Large", t("budget.large", {}, "大")],
    ["Custom", t("budget.custom", {}, "自定义")],
  ].forEach(([name, label]) => {
    const option = el("option", "", label);
    option.value = name;
    budget.appendChild(option);
  });
  budget.value = saved.budget || "Medium";
  budgetLabel.append(el("span", "", t("context.budget", {}, "预算")), budget);
  controls.append(startLabel, endLabel, budgetLabel);

  const custom = el("div", "context-review-grid custom-budget-grid");
  custom.id = "context-review-custom-budget";
  [
    ["total-characters", t("context.characterLimit", {}, "字符上限"), saved.totalCharacters, 512, 50000],
    ["estimated-tokens", t("context.tokenLimit", {}, "Token 估算上限"), saved.estimatedTokens, 128, 12500],
    ["max-evidence", t("context.evidenceLimit", {}, "历史证据上限"), saved.maxEvidence, 0, 100],
    ["max-candidates", t("context.candidateLimit", {}, "候选上限"), saved.maxCandidates, 0, 100],
    ["max-history-expansions", t("context.historyExpansionLimit", {}, "历史展开上限"), saved.maxHistoryExpansions, 0, 20],
  ].forEach(([id, label, value, min, max]) => {
    const wrapper = el("label", "control-label");
    wrapper.append(el("span", "", label), numberInput(`context-review-${id}`, value, min, max));
    custom.appendChild(wrapper);
  });
  const updateCustomVisibility = () => custom.classList.toggle("hidden", budget.value !== "Custom");
  budget.addEventListener("change", updateCustomVisibility);
  updateCustomVisibility();

  const actions = el("div", "actions");
  const assemble = button(saved.data
    ? t("context.reassemble", {}, "应用临时调整并重新装配")
    : t("context.assemble", {}, "装配上下文"));
  assemble.id = "context-review-run";
  actions.appendChild(assemble);
  const message = el(
    "p",
    "form-message",
    t(
      "legacy.tools.context.readonlySourceFlags",
      {},
      "未持久化 · 未发送至模型 · 固定 real-readonly 数据源",
    ),
  );
  message.id = "context-review-message";
  panel.append(queryLabel, controls, custom, actions, message);

  const results = el("section", "content context-review-results");
  results.id = "context-review-results";
  content.append(panel, results);
  assemble.addEventListener("click", runContextReview);
  query.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) runContextReview();
  });
  if (saved.data) renderContextReviewResults(saved.data);
  query.focus();
}

function currentContextReviewControls() {
  return {
    query: document.querySelector("#context-review-query")?.value.trim() || "",
    startTime: document.querySelector("#context-review-start")?.value || "",
    endTime: document.querySelector("#context-review-end")?.value || "",
    budget: document.querySelector("#context-review-budget")?.value || "Medium",
    totalCharacters: Number(document.querySelector("#context-review-total-characters")?.value || 6000),
    estimatedTokens: Number(document.querySelector("#context-review-estimated-tokens")?.value || 1500),
    maxEvidence: Number(document.querySelector("#context-review-max-evidence")?.value || 12),
    maxCandidates: Number(document.querySelector("#context-review-max-candidates")?.value || 6),
    maxHistoryExpansions: Number(document.querySelector("#context-review-max-history-expansions")?.value || 0),
  };
}

async function runContextReview() {
  const next = currentContextReviewControls();
  const message = document.querySelector("#context-review-message");
  const results = document.querySelector("#context-review-results");
  if (!next.query) {
    const required = t("context.queryRequired", {}, "请输入查询 / 当前请求。");
    setMessage(message, required, "error");
    renderEmpty(required, results);
    return;
  }
  const previous = state.lastContextReview;
  const assemblyControlsChanged = ["query", "startTime", "endTime", "budget", "totalCharacters", "estimatedTokens", "maxEvidence", "maxCandidates", "maxHistoryExpansions"]
    .some((key) => String(previous[key] ?? "") !== String(next[key] ?? ""));
  const excludedItemIds = assemblyControlsChanged ? [] : [...(previous.excludedItemIds || [])].sort();
  const params = new URLSearchParams({ q: next.query, budget: next.budget });
  if (next.startTime) params.set("start_time", next.startTime);
  if (next.endTime) params.set("end_time", next.endTime);
  if (next.budget === "Custom") {
    params.set("total_characters", String(next.totalCharacters));
    params.set("estimated_tokens", String(next.estimatedTokens));
    params.set("max_evidence", String(next.maxEvidence));
    params.set("max_candidates", String(next.maxCandidates));
    params.set("max_history_expansions", String(next.maxHistoryExpansions));
  }
  excludedItemIds.forEach((itemId) => params.append("exclude", itemId));
  setMessage(message, assemblyControlsChanged && previous.data
    ? t(
      "context.controlsChanged",
      {},
      "控制项已变化，已清除旧 Assembly 的临时排除，正在重新装配…",
    )
    : t("context.reassemblingReadonly", {}, "正在重新装配只读 Delivery…"));
  results.replaceChildren(el(
    "div",
    "panel",
    t(
      "context.assembling",
      {},
      "正在装配上下文…",
    ),
  ));
  try {
    const data = await api(`/api/context-delivery-preview?${params.toString()}`);
    state.lastContextReview = { ...next, excludedItemIds, data };
    renderContextReviewResults(data);
    const runButton = document.querySelector("#context-review-run");
    if (runButton) {
      runButton.textContent = t("context.reassemble", {}, "应用临时调整并重新装配");
    }
    setMessage(
      message,
      t("context.deliveryUpdated", {}, "Delivery 已更新；没有保存、发送或调用模型。"),
      "success",
    );
  } catch (error) {
    state.lastContextReview = { ...previous, ...next, excludedItemIds, data: null };
    renderTechnicalError(error, results);
    setTechnicalMessage(message, error, "error.loadFailedRetry", "内容加载失败，请重试。");
  }
}

function renderContextReviewResults(data) {
  const results = document.querySelector("#context-review-results");
  if (!results) return;
  results.innerHTML = "";
  const execution = data.execution || {};
  const budget = data.budget || {};
  const overview = el("section", "panel context-delivery-overview");
  const identity = el("div", "context-identity-grid");
  identity.append(
    metaItem(t("context.deliveryIdentityLabel", {}, "Delivery identity："), data.delivery_identity || ""),
    metaItem(t("context.assemblyIdentityLabel", {}, "Assembly identity："), data.assembly_binding?.assembly_identity || ""),
    metaItem(t("common.statusLabel", {}, "状态："), `${enumLabel("status", data.status || "")} (${data.reason_code || ""})`),
    metaItem(t("context.reviewStateLabel", {}, "审查状态："), enumLabel("status", data.review_state || "")),
  );
  const metrics = el("div", "summary-health-grid prompt-metrics");
  metrics.append(
    metricCard(
      t("context.approvedProfile", {}, "已批准画像"),
      `${formatUiNumber(data.counts?.approved_profile_included ?? 0)}/${formatUiNumber(data.counts?.approved_profile_available ?? 0)}`,
    ),
    metricCard(
      t("context.historicalEvidence", {}, "历史证据"),
      formatUiNumber(data.counts?.historical_evidence_included ?? 0),
    ),
    metricCard(
      t("legacy.tools.context.lexicalMatches", {}, "词法原始匹配"),
      formatUiNumber(data.counts?.lexical_raw_matches ?? 0),
    ),
    metricCard(
      t("legacy.tools.context.characters", {}, "字符"),
      `${formatUiNumber(budget.used_characters ?? 0)}/${formatUiNumber(budget.total_characters_limit ?? 0)}`,
    ),
    metricCard(
      t("legacy.tools.context.estimatedTokens", {}, "预计 Token"),
      `${formatUiNumber(budget.estimated_tokens_used ?? 0)}/${formatUiNumber(budget.estimated_token_limit ?? 0)}`,
    ),
    metricCard(t("runtime.persisted", {}, "已持久化"), runtimeBoolean(execution.persisted)),
    metricCard(t("runtime.sentToModel", {}, "已发送至模型"), runtimeBoolean(execution.sent_to_model)),
    metricCard(t("runtime.externalModelCalls", {}, "外部模型调用"), runtimeBoolean(execution.external_model_calls)),
    metricCard(t("runtime.credentialRead", {}, "读取凭据"), runtimeBoolean(execution.credential_read)),
    metricCard(
      t("legacy.tools.context.exclusionRequested", {}, "已请求临时排除"),
      formatUiNumber(data.counts?.temporary_exclusion_requested ?? 0),
    ),
    metricCard(
      t("legacy.tools.context.temporaryExcluded", {}, "临时排除"),
      formatUiNumber(data.counts?.temporary_excluded ?? 0),
    ),
    metricCard(
      t("legacy.tools.context.cascadeExcluded", {}, "来源级联排除"),
      formatUiNumber(data.counts?.provenance_cascade_excluded ?? 0),
    ),
    metricCard(
      t("legacy.tools.context.omitted", {}, "已省略"),
      formatUiNumber(data.counts?.omitted_manifest_entries ?? 0),
    ),
  );
  const identityActions = el("div", "actions");
  const copyDelivery = button(t("context.copyDeliveryIdentity", {}, "复制 Delivery identity"), "button secondary");
  copyDelivery.addEventListener("click", () => copyText(data.delivery_identity || "", copyDelivery));
  const copyAssembly = button(t("context.copyAssemblyIdentity", {}, "复制 Assembly identity"), "button secondary");
  copyAssembly.addEventListener("click", () => copyText(data.assembly_binding?.assembly_identity || "", copyAssembly));
  identityActions.append(copyDelivery, copyAssembly);
  overview.append(
    el("h3", "section-title", t("context.deliveryEnvelope", {}, "Delivery envelope")),
    identity,
    metrics,
    identityActions,
  );
  results.appendChild(overview);

  const capability = data.capability_state || {};
  const capabilityPanel = el("details", "panel context-layer");
  const capabilitySummary = el(
    "summary",
    "context-layer-summary",
    t(
      "context.capabilitySummary",
      {
        status: enumLabel("status", capability.status || "unknown"),
        reason: capability.reason_code || "",
      },
      "能力与降级 · {status}（{reason}）",
    ),
  );
  capabilityPanel.appendChild(capabilitySummary);
  const missing = (capability.missing_capabilities || []).join(", ")
    || t("legacy.tools.common.none", {}, "无");
  capabilityPanel.append(
    el(
      "p",
      "snippet",
      t(
        "legacy.tools.context.capabilityDetail",
        {
          degradation: enumLabel("status", capability.degradation?.status || "unknown"),
          missing,
        },
        "降级状态：{degradation} · 缺失能力：{missing}",
      ),
    ),
    el("pre", "context-json-preview", JSON.stringify({ capabilities: capability.capabilities, lexical_fallback: capability.lexical_fallback, warnings: capability.warnings }, null, 2)),
  );
  results.appendChild(capabilityPanel);

  const layerMeta = [
    ["current_task", t("context.layerCurrentTask", {}, "A · 当前任务")],
    ["approved_profile", t("context.layerApprovedProfile", {}, "B · 已批准画像")],
    ["candidate_memory", t("context.layerCandidate", {}, "C · 候选 / 未批准")],
    ["historical_evidence", t("context.layerHistoricalEvidence", {}, "D · 历史证据")],
  ];
  layerMeta.forEach(([layerName, label], index) => {
    const layer = data.layers?.[layerName] || {};
    const items = (data.review_items || []).filter((item) => {
      const normalized = item.layer === "confirmed_user_profile" ? "approved_profile" : item.layer;
      return normalized === layerName;
    });
    const details = el("details", "panel context-layer");
    if (index < 2) details.open = true;
    details.appendChild(el(
      "summary",
      "context-layer-summary",
      t(
        "legacy.tools.context.layerCountSummary",
        {
          label,
          included: formatUiNumber(items.filter((item) => item.included).length),
          total: formatUiNumber(items.length),
        },
        "{label} · 已纳入 {included}/{total}",
      ),
    ));
    if (layerName === "approved_profile" && layer.status === "empty") {
      details.appendChild(el(
        "div",
        "empty-card approved-profile-empty",
        layer.empty_message || t("profile.noActiveProfile", {}, "没有已生效的批准画像。"),
      ));
    }
    if (!items.length) {
      details.appendChild(el(
        "div",
        "empty-card",
        t("context.noReviewableItems", {}, "本层没有可审查条目。"),
      ));
    }
    items.forEach((item) => details.appendChild(contextReviewItemCard(item)));
    results.appendChild(details);
  });

  const omittedPanel = el("details", "panel context-layer omitted-panel");
  omittedPanel.appendChild(el(
    "summary",
    "context-layer-summary",
    t(
      "context.omittedCount",
      { count: formatUiNumber((data.omitted || []).length) },
      "遗漏清单 · {count}",
    ),
  ));
  if (!(data.omitted || []).length) {
    omittedPanel.appendChild(el(
      "div",
      "empty-card",
      t("context.noOmittedItems", {}, "没有遗漏条目。"),
    ));
  }
  (data.omitted || []).forEach((item) => {
    omittedPanel.appendChild(el(
      "p",
      "omitted-line",
      t(
        "legacy.tools.context.omittedItemDetail",
        {
          layer: item.layer,
          type: item.item_type,
          id: item.item_id,
          reason: item.reason,
          origin: item.origin,
        },
        "{layer}/{type}/{id} · {reason} · {origin}",
      ),
    ));
  });
  results.appendChild(omittedPanel);

  const uncertaintyPanel = el("details", "panel context-layer uncertainty-panel");
  uncertaintyPanel.open = true;
  uncertaintyPanel.appendChild(el(
    "summary",
    "context-layer-summary",
    t(
      "context.uncertaintyCount",
      { count: formatUiNumber((data.uncertainties || []).length) },
      "遗漏 / 不确定信息 · {count}",
    ),
  ));
  if (!(data.uncertainties || []).length) {
    uncertaintyPanel.appendChild(el(
      "div",
      "empty-card",
      t("context.noUncertainty", {}, "没有明确的不确定信息。"),
    ));
  }
  (data.uncertainties || []).forEach((item) => {
    uncertaintyPanel.appendChild(el(
      "p",
      "omitted-line",
      t(
        "legacy.tools.context.uncertaintyDetail",
        { reason: item.reason_code, statement: item.statement },
        "{reason} · {statement}",
      ),
    ));
  });
  results.appendChild(uncertaintyPanel);

  const modelPanel = el("section", "panel final-model-context");
  const modelHeader = el("div", "detail-header");
  modelHeader.appendChild(el(
    "h3",
    "section-title",
    t("context.finalModelContext", {}, "最终模型上下文"),
  ));
  const copyContext = button(t("context.copyModelContext", {}, "复制模型上下文"), "button secondary");
  copyContext.addEventListener("click", () => copyText(data.model_context || "", copyContext));
  modelHeader.appendChild(copyContext);
  const preview = el("pre", "model-context-preview", data.model_context || "");
  modelPanel.append(modelHeader, preview);
  results.appendChild(modelPanel);
}

function contextReviewItemCard(item) {
  const card = el("article", `context-review-item ${item.included ? "is-included" : "is-omitted"}`);
  const top = el("div", "context-review-item-top");
  const includeLabel = el("label", "context-include-toggle");
  const exclusion = item.exclusion || {};
  const cascadeExcluded = exclusion.state === "provenance_cascade";
  const checkbox = checkboxInput(`context-item-${item.item_id}`, item.included);
  checkbox.disabled = !item.excludable || cascadeExcluded;
  const exclusionLabel = cascadeExcluded
    ? t("context.excludeCascade", {}, "排除（来源级联）")
    : exclusion.state === "requested"
      ? t("context.excludeRequested", {}, "排除（已请求）")
      : t("context.include", {}, "纳入");
  includeLabel.append(checkbox, el("span", "", exclusionLabel));
  checkbox.addEventListener("change", () => {
    const excluded = new Set(state.lastContextReview.excludedItemIds || []);
    if (checkbox.checked) excluded.delete(item.item_id);
    else excluded.add(item.item_id);
    state.lastContextReview.excludedItemIds = [...excluded].sort();
    includeLabel.querySelector("span").textContent = checkbox.checked
      ? t("context.include", {}, "纳入")
      : t("context.exclude", {}, "排除");
    setMessage(
      document.querySelector("#context-review-message"),
      t(
        "context.exclusionChanged",
        {},
        "临时排除已修改；点击“应用临时调整并重新装配”生成新的 Delivery identity。",
      ),
      "success",
    );
  });
  top.append(
    includeLabel,
    el("span", "status", enumLabel("status", item.trust_status || "")),
    el(
      "span",
      "meta",
      t(
        "context.itemSize",
        {
          characters: formatUiNumber(item.characters ?? 0),
          tokens: formatUiNumber(item.estimated_tokens ?? 0),
        },
        "{characters} 个字符 · 约 {tokens} Token",
      ),
    ),
  );
  const idLine = el("p", "reference-uri", `${item.item_type} · ${item.item_id}`);
  const body = el(
    "p",
    "summary-text",
    item.content || t(
      "legacy.tools.context.unprojectedBody",
      {},
      "（原装配内容未投影正文）",
    ),
  );
  card.append(top, idLine, body);
  const provenance = item.provenance || {};
  card.appendChild(el(
    "p",
    "reference-uri",
    t(
      "legacy.tools.context.provenanceDetail",
      {
        sources: (provenance.source_ids || []).join(",")
          || t("legacy.tools.common.none", {}, "无"),
        evidence: (provenance.evidence_ids || []).join(",")
          || t("legacy.tools.common.none", {}, "无"),
        events: (provenance.event_ids || []).join(",")
          || t("legacy.tools.common.none", {}, "无"),
      },
      "来源：{sources} · 证据：{evidence} · 事件：{events}",
    ),
  ));
  if (cascadeExcluded) {
    card.appendChild(el(
      "p",
      "omitted-reason",
      t(
        "legacy.tools.context.cascadeSeeds",
        {
          seeds: (exclusion.caused_by_item_ids || []).join(", ")
            || t("common.unknown", {}, "未知"),
        },
        "级联来源：{seeds}",
      ),
    ));
  }
  if ((item.omitted_reasons || []).length) {
    card.appendChild(el(
      "p",
      "omitted-reason",
      t(
        "legacy.tools.context.omittedReasons",
        { reasons: item.omitted_reasons.join(", ") },
        "省略原因：{reasons}",
      ),
    ));
  }
  if (item.source_anchor?.conversation_id && item.source_anchor?.message_id) {
    const actions = el("div", "actions");
    const source = button(t("context.openSourceMessage", {}, "跳转来源消息"), "button secondary");
    source.addEventListener("click", () => loadContext({
      conversation_id: item.source_anchor.conversation_id,
      message_id: item.source_anchor.message_id,
      title: t("context.sourceMessage", {}, "来源消息"),
      reference_uri: item.source_anchor.reference_uri,
    }, state.lastContextReview.query));
    const copy = button(t("context.copySourceAnchor", {}, "复制 Source Anchor"), "button secondary");
    copy.addEventListener("click", () => copyText(item.source_anchor.reference_uri || "", copy));
    actions.append(source, copy);
    card.appendChild(actions);
  }
  return card;
}

function profileMutationId(prefix) {
  const value = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${value}`;
}

function legacyProfileTypeLabel(value) {
  const raw = String(value || "").trim();
  const translations = {
    fact: ["legacy.tools.profile.type.fact", "事实"],
    preference: ["legacy.tools.profile.type.preference", "偏好"],
    goal: ["legacy.tools.profile.type.goal", "目标"],
    decision: ["legacy.tools.profile.type.decision", "决策"],
    constraint: ["legacy.tools.profile.type.constraint", "约束"],
    open_question: ["legacy.tools.profile.type.openQuestion", "待确认问题"],
    acceptable_alternative: ["legacy.tools.profile.type.acceptableAlternative", "可接受替代项"],
  };
  if (translations[raw]) return t(translations[raw][0], {}, translations[raw][1]);
  return raw || t("profile.candidate", {}, "候选");
}

function legacyProfileWriteModeLabel(value) {
  const raw = String(value || "").trim();
  if (raw === "profile_admin") {
    return t("legacy.tools.profile.writeMode.admin", {}, "画像管理");
  }
  if (raw === "readonly_preview") {
    return t("legacy.tools.profile.writeMode.readonlyPreview", {}, "只读预览");
  }
  return raw || t("common.unknown", {}, "未知");
}

const SEALED_APPROVED_PROFILE_DECISION_ENDPOINT = "/api/approved-profile-decision";
const SEALED_APPROVED_PROFILE_CONTROL_ENDPOINT = "/api/approved-profile-control";

function loadApprovedProfiles() {
  setActive("approvedProfiles");
  setTitle(t("profile.title", {}, "画像审批"));
  content.innerHTML = "";
  appendReadonlyNotice();
  const saved = state.lastApprovedProfileControl;
  const controls = el("section", "panel profile-control-panel");
  const query = document.createElement("input");
  query.id = "approved-profile-query";
  query.placeholder = t(
    "profile.queryPlaceholder",
    {},
    "输入关键词，查看经过 v7.2 验证的候选项",
  );
  query.setAttribute(
    "aria-label",
    t("legacy.tools.profile.candidateQueryLabel", {}, "候选项查询"),
  );
  query.value = saved.query || "";
  const budget = document.createElement("select");
  budget.id = "approved-profile-budget";
  budget.setAttribute(
    "aria-label",
    t("legacy.tools.profile.budgetLabel", {}, "预算档位"),
  );
  [
    ["Small", t("budget.small", {}, "小")],
    ["Medium", t("budget.medium", {}, "中")],
    ["Large", t("budget.large", {}, "大")],
  ].forEach(([value, label]) => {
    const option = el("option", "", label);
    option.value = value;
    budget.appendChild(option);
  });
  budget.value = saved.budget || "Medium";
  const run = button(t("profile.viewCandidates", {}, "查看候选项"));
  run.id = "approved-profile-run";
  const message = el("p", "meta");
  message.id = "approved-profile-message";
  const row = el("div", "profile-control-query");
  row.append(query, budget, run);
  const proposalText = document.createElement("textarea");
  proposalText.id = "direct-profile-proposal-text";
  proposalText.maxLength = 500;
  proposalText.placeholder = t(
    "profile.directProposalPlaceholder",
    {},
    "直接用户提议（绝不自动批准）",
  );
  proposalText.setAttribute(
    "aria-label",
    t("legacy.tools.profile.directProposalTextLabel", {}, "直接提议正文"),
  );
  const proposalType = document.createElement("select");
  proposalType.id = "direct-profile-proposal-type";
  proposalType.setAttribute(
    "aria-label",
    t("legacy.tools.profile.proposalTypeLabel", {}, "提议类型"),
  );
  ["fact", "preference", "goal", "decision", "constraint"].forEach((value) => {
    const option = el("option", "", legacyProfileTypeLabel(value));
    option.value = value;
    proposalType.appendChild(option);
  });
  const proposalAnchors = document.createElement("textarea");
  proposalAnchors.id = "direct-profile-proposal-anchors";
  proposalAnchors.placeholder = t(
    "profile.optionalSourceAnchors",
    {},
    "可选：每行一个 conversation_id|message_id",
  );
  proposalAnchors.setAttribute(
    "aria-label",
    t("legacy.tools.profile.sourceAnchorsLabel", {}, "来源锚点"),
  );
  const createProposal = button(
    t("profile.recordProposalOnly", {}, "Gate B1：仅记录提议"),
    "button secondary",
  );
  createProposal.id = "direct-profile-proposal-create";
  const proposalPanel = el("section", "profile-direct-proposal");
  proposalPanel.append(
    el("h4", "card-title", t("profile.directProposal", {}, "v8.0-D1 直接用户提议")),
    el(
      "p",
      "meta",
      t(
        "profile.gateB1Description",
        {},
        "Gate B1 只记录一条提议并停止。重新加载并检查返回的 ID 后，再单独授权 Gate B2。",
      ),
    ),
    proposalText, proposalType, proposalAnchors, createProposal,
  );
  controls.append(
    el("h3", "section-title", t("profile.control", {}, "v8.0-A Lite 已批准画像控制")),
    el(
      "p",
      "meta",
      t(
        "profile.controlDescription",
        {},
        "只读取封版 v7.2 候选项与来源证据；决策仅写入独立的 append-only sidecar，不写真实聊天库。",
      ),
    ),
    row,
    proposalPanel,
    message,
  );
  const results = el("section", "content approved-profile-results");
  results.id = "approved-profile-results";
  content.append(controls, results);
  run.addEventListener("click", runApprovedProfileReview);
  createProposal.addEventListener("click", submitDirectProfileProposal);
  query.addEventListener("keydown", (event) => {
    if (event.key === "Enter") runApprovedProfileReview();
  });
  if (saved.data) renderApprovedProfileReview(saved.data);
}

async function runApprovedProfileReview() {
  const query = document.querySelector("#approved-profile-query")?.value.trim() || "";
  const budget = document.querySelector("#approved-profile-budget")?.value || "Medium";
  const message = document.querySelector("#approved-profile-message");
  const results = document.querySelector("#approved-profile-results");
  if (!query) {
    setMessage(message, t("profile.enterCandidateQuery", {}, "请输入候选项查询关键词。"), "error");
    return;
  }
  setMessage(
    message,
    t("profile.loadingCandidates", {}, "正在重建可追溯来源的 v7.2 候选项…"),
  );
  results.replaceChildren(el(
    "div",
    "panel",
    t(
      "profile.readingCandidates",
      {},
      "正在读取候选画像…",
    ),
  ));
  try {
    const params = new URLSearchParams({ q: query, budget });
    const data = await api(`/api/profile-enablement-control?${params.toString()}`);
    state.lastApprovedProfileControl = { query, budget, data };
    renderApprovedProfileReview(data);
    setMessage(message, data.mutation_available
      ? t(
        "profile.adminLoaded",
        { revision: data.control_revision },
        "已加载修订版 {revision}；当前为显式 profile-admin 模式。",
      )
      : data.sidecar?.status === "ready"
        ? t(
          "profile.readonlyLoaded",
          {},
          "候选项已加载；当前为只读预览，审批 mutation 已禁用。",
        )
        : t(
          "profile.sidecarUnavailable",
          {},
          "候选项已加载；生产 sidecar 尚未初始化，因此决策按钮已禁用。",
        ));
  } catch (error) {
    state.lastApprovedProfileControl = { query, budget, data: null };
    results.innerHTML = "";
    setTechnicalMessage(message, error, "error.loadFailedRetry", "内容加载失败，请重试。");
  }
}

function renderApprovedProfileReview(data) {
  const results = document.querySelector("#approved-profile-results");
  if (!results) return;
  results.innerHTML = "";
  const overview = el("section", "panel profile-control-overview");
  const metrics = el("div", "prompt-metrics");
  metrics.append(
    metricCard(
      t("legacy.tools.profile.controlRevision", {}, "控制修订版"),
      formatUiNumber(data.control_revision ?? 0),
    ),
    metricCard(
      t("legacy.tools.profile.candidateCount", {}, "候选项"),
      formatUiNumber((data.candidates || []).length),
    ),
    metricCard(
      t("legacy.tools.profile.directProposalCount", {}, "直接提议"),
      formatUiNumber((data.direct_proposals || []).length),
    ),
    metricCard(
      t("legacy.tools.profile.activeProfileCount", {}, "生效画像"),
      formatUiNumber(data.active_approved_profile?.items?.length || 0),
    ),
    metricCard(
      t("legacy.tools.profile.sidecarStatus", {}, "伴随库状态"),
      enumLabel("status", data.sidecar?.status || "unknown"),
    ),
    metricCard(
      t("legacy.tools.profile.writeMode", {}, "写入模式"),
      legacyProfileWriteModeLabel(data.write_mode || "readonly_preview"),
    ),
  );
  overview.append(
    el("h3", "section-title", t("profile.controlStatus", {}, "控制面状态")),
    metrics,
    el(
      "p",
      "reference-uri",
      data.source?.package_identity || t("profile.noPackageIdentity", {}, "没有状态包标识"),
    ),
  );
  results.appendChild(overview);

  const active = el("section", "panel");
  active.appendChild(el(
    "h3",
    "section-title",
    t("profile.active", {}, "已生效的批准画像投影"),
  ));
  const activeItems = data.active_approved_profile?.items || [];
  if (!activeItems.length) {
    active.appendChild(el(
      "p",
      "empty-card",
      t("profile.noActiveProfile", {}, "当前没有已生效的批准画像。"),
    ));
  } else {
    activeItems.forEach((item) => {
      const card = el("article", "card active-profile-card");
      card.append(
        el(
          "h4",
          "card-title",
          t(
            "legacy.tools.profile.itemHeading",
            {
              kind: legacyProfileTypeLabel(item.kind),
              id: item.profile_item_id,
            },
            "{kind} · {id}",
          ),
        ),
        el("p", "", item.statement || ""),
        el(
          "p",
          "meta",
          t(
            "legacy.tools.profile.approvalMeta",
            {
              approvedAt: item.approval?.approved_at
                ? formatUiDateTime(item.approval.approved_at)
                : t("common.notRecorded", {}, "未记录"),
              modelVisible: runtimeBoolean(item.model_visible === true),
            },
            "批准时间：{approvedAt} · 模型可见：{modelVisible}",
          ),
        ),
      );
      active.appendChild(card);
    });
  }
  results.appendChild(active);

  const history = el("section", "panel profile-decision-history");
  history.appendChild(el(
    "h3",
    "section-title",
    t("profile.decisionHistory", {}, "仅追加决策历史"),
  ));
  const decisions = data.decision_history || [];
  if (!decisions.length) {
    history.appendChild(el(
      "p",
      "empty-card",
      t("profile.noDecisionHistory", {}, "尚无手动画像决策。"),
    ));
  } else {
    decisions.forEach((decision) => {
      decision.candidate_id = decision.subject_id;
      decision.candidate = { statement: decision.final_text || decision.original_text || "" };
      const card = el("article", "card profile-decision-history-card");
      card.append(
        el(
          "h4",
          "card-title",
          t(
            "profile.decisionRevision",
            {
              action: enumLabel("decisionAction", decision.action),
              revision: decision.resulting_revision,
            },
            "{action} · 修订版 {revision}",
          ),
        ),
        el(
          "p",
          "meta",
          t(
            "legacy.tools.profile.decisionSubjectMeta",
            {
              source: decision.decision_source,
              candidate: decision.candidate_id,
            },
            "决策来源：{source} · 候选项：{candidate}",
          ),
        ),
        el("p", "", decision.edited_statement || decision.candidate?.statement || ""),
        el(
          "p",
          "meta",
          t(
            "legacy.tools.profile.decisionRecordMeta",
            {
              id: decision.decision_id,
              decidedAt: decision.decided_at
                ? formatUiDateTime(decision.decided_at)
                : t("common.notRecorded", {}, "未记录"),
            },
            "决策 ID：{id} · 决策时间：{decidedAt}",
          ),
        ),
      );
      if (decision.reason) {
        card.appendChild(el(
          "p",
          "meta",
          t(
            "legacy.tools.profile.decisionReason",
            { reason: decision.reason },
            "原因：{reason}",
          ),
        ));
      }
      history.appendChild(card);
    });
  }
  results.appendChild(history);

  const proposals = el("section", "profile-candidate-list");
  proposals.appendChild(el(
    "h3",
    "section-title",
    t("profile.directProposals", {}, "直接用户提议"),
  ));
  (data.direct_proposals || []).forEach((entry) => {
    proposals.appendChild(directProfileProposalCard(entry, data));
  });
  if (!(data.direct_proposals || []).length) {
    proposals.appendChild(el(
      "div",
      "panel empty-card",
      t("profile.noDirectProposals", {}, "没有直接用户提议。"),
    ));
  }
  results.appendChild(proposals);

  const list = el("section", "profile-candidate-list");
  (data.candidates || []).forEach((entry) => list.appendChild(approvedProfileCandidateCard(entry, data)));
  if (!(data.candidates || []).length) {
    list.appendChild(el(
      "div",
      "panel empty-card",
      t("profile.noCandidates", {}, "没有匹配的 v7.2 候选项。"),
    ));
  }
  results.appendChild(list);
}

function directProfileProposalCard(entry, review) {
  const proposal = entry.proposal || {};
  const proposalType = legacyProfileTypeLabel(proposal.proposal_type);
  const card = el("article", "panel profile-candidate-card");
  card.append(
    el(
      "span",
      "mode-pill",
      t(
        "legacy.tools.profile.directProposalType",
        { type: proposalType },
        "直接用户提议 / {type}",
      ),
    ),
    el("h3", "card-title", proposal.original_text || ""),
    el(
      "p",
      "meta",
      t(
        "legacy.tools.profile.proposalOrigin",
        {
          id: proposal.proposal_id || t("common.unknown", {}, "未知"),
          author: proposal.proposed_by || t("common.unknown", {}, "未知"),
        },
        "{id} · 提议者：{author}",
      ),
    ),
    el(
      "p",
      "meta",
      t(
        "legacy.tools.profile.sourceAnchors",
        {
          anchors: (proposal.source_anchors || [])
            .map((item) => `${item.conversation_id}+${item.message_id}`)
            .join(", ") || t("legacy.tools.common.none", {}, "无"),
        },
        "来源锚点：{anchors}",
      ),
    ),
  );
  const edit = document.createElement("textarea");
  edit.className = "profile-edit-statement";
  edit.value = proposal.original_text || "";
  edit.maxLength = 500;
  edit.setAttribute(
    "aria-label",
    t("legacy.tools.profile.editDirectProposalLabel", {}, "批准前编辑直接提议"),
  );
  const reason = document.createElement("input");
  reason.className = "profile-decision-reason";
  reason.maxLength = 300;
  reason.placeholder = t("profile.optionalDecisionReason", {}, "可选决策理由");
  reason.setAttribute(
    "aria-label",
    t("profile.optionalDecisionReason", {}, "可选决策理由"),
  );
  const actions = el("div", "actions profile-decision-actions");
  (entry.allowed_actions || []).forEach((action) => {
    const trigger = button(
      enumLabel("decisionAction", action),
      action === "reject" ? "button secondary" : "button",
    );
    trigger.disabled = !review.mutation_available;
    trigger.addEventListener("click", () => submitProfileEnablementDecision(entry, action, edit.value, reason.value));
    actions.appendChild(trigger);
  });
  card.append(edit, reason, actions);
  return card;
}

function approvedProfileCandidateCard(entry, review) {
  const candidate = entry.candidate || {};
  const evidence = entry.evidence || {};
  const anchor = evidence.source_anchor || {};
  const card = el("article", "panel profile-candidate-card");
  const header = el("div", "detail-header");
  const titleBlock = el("div");
  titleBlock.append(
    el(
      "span",
      "mode-pill",
      t(
        "legacy.tools.profile.candidateTypeStatus",
        {
          type: legacyProfileTypeLabel(candidate.candidate_type),
          status: enumLabel("status", candidate.review_status || ""),
        },
        "{type} / {status}",
      ),
    ),
    el("h3", "card-title", candidate.statement || ""),
    el("p", "meta", candidate.candidate_id || ""),
  );
  const status = entry.current_decision
    ? t(
      "profile.decisionRevision",
      {
        action: enumLabel("decisionAction", entry.current_decision.action),
        revision: entry.current_decision.resulting_revision,
      },
      "{action} · 修订版 {revision}",
    )
    : t("profile.noDecision", {}, "尚无决策");
  header.append(titleBlock, el("span", entry.active ? "health-pass" : "health-badge muted", status));
  card.appendChild(header);

  const evidenceDetails = el("details", "profile-evidence");
  evidenceDetails.appendChild(el(
    "summary",
    "",
    t("profile.viewSourceEvidence", {}, "查看来源证据"),
  ));
  const evidenceGrid = el("div", "meta-grid");
  evidenceGrid.append(
    metaItem(
      t("legacy.tools.profile.conversationIdLabel", {}, "会话 ID："),
      anchor.conversation_id || "",
    ),
    metaItem(
      t("legacy.tools.profile.messageIdLabel", {}, "消息 ID："),
      anchor.message_id || "",
    ),
    metaItem(t("legacy.tools.profile.roleLabel", {}, "角色："), anchor.role || ""),
    metaItem(
      t("legacy.tools.profile.observedAtLabel", {}, "观测时间："),
      evidence.observed_at_utc
        ? formatUiDateTime(evidence.observed_at_utc)
        : t("common.notRecorded", {}, "未记录"),
    ),
    metaItem(
      t("legacy.tools.profile.evidenceIdsLabel", {}, "证据 ID："),
      (evidence.evidence_ids || []).join(", "),
    ),
    metaItem(
      t("legacy.tools.profile.excerptSha256Label", {}, "摘录 SHA-256："),
      evidence.source_excerpt_sha256 || "",
    ),
  );
  const sourceButton = button(
    t("profile.openSourceMessage", {}, "打开来源消息"),
    "button secondary small",
  );
  sourceButton.disabled = !anchor.conversation_id || !anchor.message_id;
  sourceButton.addEventListener("click", () => loadContext({
    conversation_id: anchor.conversation_id,
    message_id: anchor.message_id,
    title: anchor.conversation_title,
    reference_uri: anchor.reference_uri,
  }, state.lastApprovedProfileControl.query));
  evidenceDetails.append(evidenceGrid, el("p", "reference-uri", anchor.reference_uri || ""), sourceButton);
  card.appendChild(evidenceDetails);

  const edit = document.createElement("textarea");
  edit.className = "profile-edit-statement";
  edit.value = candidate.statement || "";
  edit.maxLength = 500;
  edit.setAttribute(
    "aria-label",
    t("profile.editApprovedStatement", {}, "编辑后批准的画像陈述"),
  );
  const reason = document.createElement("input");
  reason.className = "profile-decision-reason";
  reason.maxLength = 300;
  reason.placeholder = t("profile.optionalDecisionReason", {}, "可选决策说明");
  reason.setAttribute(
    "aria-label",
    t("profile.optionalDecisionReason", {}, "可选决策说明"),
  );
  const actions = el("div", "actions profile-decision-actions");
  (entry.allowed_actions || []).forEach((action) => {
    const trigger = button(
      enumLabel("decisionAction", action),
      action === "reject" ? "button secondary" : "button",
    );
    trigger.disabled = !review.mutation_available;
    trigger.addEventListener("click", () => submitProfileEnablementDecision(entry, action, edit.value, reason.value));
    actions.appendChild(trigger);
  });
  card.append(edit, reason, actions);
  return card;
}

async function submitApprovedProfileDecision(entry, action, editedStatement, reason) {
  const review = state.lastApprovedProfileControl.data;
  const message = document.querySelector("#approved-profile-message");
  if (!review?.mutation_available) {
    setMessage(message, t("profile.sidecarUnavailable", {}, "已批准画像 sidecar 尚未初始化。"), "error");
    return;
  }
  const payload = {
    q: state.lastApprovedProfileControl.query,
    budget: state.lastApprovedProfileControl.budget,
    package_identity: review.source.package_identity,
    candidate_id: entry.candidate.candidate_id,
    action,
    expected_revision: review.control_revision,
    request_id: profileMutationId("request"),
    mutation_id: profileMutationId("mutation"),
    edited_statement: action === "edit_and_approve" ? editedStatement : "",
    reason: reason || "",
  };
  setMessage(message, t("profile.writingDecision", {}, "正在写入仅追加决策记录…"));
  try {
    const response = await fetch("/api/approved-profile-decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (response.status === 409 && data.reason_code === "expected_revision_conflict") {
      setMessage(
        message,
        t(
          "profile.stalePageRevision",
          { revision: data.current_revision },
          "页面已过期；当前修订版为 {revision}，正在刷新。",
        ),
        "error",
      );
      await runApprovedProfileReview();
      return;
    }
    if (!response.ok) {
      throw new Error(data.error || t("profile.decisionFailed", {}, "画像决策失败。"));
    }
    await runApprovedProfileReview();
  } catch (error) {
    setTechnicalMessage(message, error);
  }
}

async function submitProfileEnablementDecision(entry, action, editedStatement, reason) {
  const review = state.lastApprovedProfileControl.data;
  const message = document.querySelector("#approved-profile-message");
  if (!review?.mutation_available) {
    setMessage(
      message,
      t(
        "profile.adminRequired",
        {},
        "画像决策需要已初始化的 D1 sidecar 和 profile-admin 模式。",
      ),
      "error",
    );
    return;
  }
  const payload = {
    subject_origin: entry.origin,
    subject_id: entry.subject_id,
    action,
    expected_revision: review.control_revision,
    request_id: profileMutationId("request"),
    mutation_id: profileMutationId("mutation"),
    edited_statement: action === "edit_and_approve" ? editedStatement : "",
    reason: reason || "",
  };
  if (entry.origin === "v7.2_candidate") {
    payload.q = state.lastApprovedProfileControl.query;
    payload.budget = state.lastApprovedProfileControl.budget;
    payload.package_identity = review.source.package_identity;
  }
  try {
    const response = await fetch("/api/profile-enablement-decision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (response.status === 409 && data.reason_code === "expected_revision_conflict") {
      setMessage(
        message,
        t(
          "profile.stalePageRevision",
          { revision: data.current_revision },
          "页面已过期；当前修订版为 {revision}，正在重新加载。",
        ),
        "error",
      );
      await runApprovedProfileReview();
      return;
    }
    if (!response.ok) {
      throw new Error(data.error || t("profile.decisionFailed", {}, "画像决策失败。"));
    }
    await runApprovedProfileReview();
  } catch (error) {
    setTechnicalMessage(message, error);
  }
}

async function submitDirectProfileProposal() {
  const review = state.lastApprovedProfileControl.data;
  const message = document.querySelector("#approved-profile-message");
  if (!review?.mutation_available) {
    setMessage(
      message,
      t(
        "profile.directProposalAdminRequired",
        {},
        "直接提议需要已初始化的 D1 sidecar 和 profile-admin 模式。",
      ),
      "error",
    );
    return;
  }
  const originalText = document.querySelector("#direct-profile-proposal-text")?.value.trim() || "";
  if (!originalText) {
    setMessage(message, t("profile.proposalTextRequired", {}, "必须填写直接提议正文。"), "error");
    return;
  }
  const lines = (document.querySelector("#direct-profile-proposal-anchors")?.value || "")
    .split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const sourceAnchors = [];
  for (const line of lines) {
    const parts = line.split("|");
    if (parts.length !== 2 || !parts[0].trim() || !parts[1].trim()) {
      setMessage(
        message,
        t(
          "profile.sourceAnchorInvalid",
          {},
          "每个 Source Anchor 必须是 conversation_id|message_id。",
        ),
        "error",
      );
      return;
    }
    sourceAnchors.push({ conversation_id: parts[0].trim(), message_id: parts[1].trim() });
  }
  try {
    const response = await fetch("/api/profile-enablement-proposal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        original_text: originalText,
        proposal_type: document.querySelector("#direct-profile-proposal-type")?.value || "fact",
        source_anchors: sourceAnchors,
        expected_revision: review.control_revision,
        request_id: profileMutationId("proposal-request"),
        mutation_id: profileMutationId("proposal-mutation"),
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || t("profile.proposalFailed", {}, "直接提议失败。"));
    }
    state.lastApprovedProfileControl.data = null;
    setMessage(
      message,
      t(
        "profile.gateB1Stopped",
        { id: data.proposal?.proposal_id || "unknown" },
        "Gate B1 已停止。proposal_id={id}。重新加载复核，并在任何决策前单独取得 Gate B2 授权。",
      ),
    );
  } catch (error) {
    setTechnicalMessage(message, error);
  }
}

function legacyPromptSectionLabel(value) {
  const raw = String(value || "").trim();
  const translations = {
    System: ["legacy.tools.context.section.system", "系统"],
    "High-level Memory Summaries - whale": [
      "legacy.tools.context.section.highLevelWhaleSummaries",
      "鲸鱼记忆摘要",
    ],
    "Relevant Memory - whale": [
      "legacy.tools.context.section.relevantWhaleMemory",
      "相关鲸鱼记忆",
    ],
    "Hybrid Relevant Memory - whale": [
      "legacy.tools.context.section.hybridRelevantWhaleMemory",
      "混合相关鲸鱼记忆",
    ],
    "Optional Context - general": [
      "legacy.tools.context.section.optionalGeneralContext",
      "可选通用上下文",
    ],
    "User Input": ["legacy.tools.context.section.userInput", "用户输入"],
  };
  if (translations[raw]) return t(translations[raw][0], {}, translations[raw][1]);
  return raw || t("context.section", {}, "区段");
}

function legacyOptimizerReason(value) {
  const raw = String(value || "");
  const prefix = "Recommended because it balances lower token cost with available whale summaries/raw memory:";
  if (!raw.startsWith(prefix)) return raw;
  return t(
    "legacy.tools.context.optimizerReason",
    { details: raw.slice(prefix.length).trim() },
    "推荐此配置，因为它兼顾较低的 Token 成本和可用的鲸鱼摘要/原始记忆：{details}",
  );
}

function loadPromptInspector() {
  setActive("promptInspector");
  setTitle(t("context.inspector", {}, "上下文检查器"));
  content.innerHTML = "";
  appendReadonlyNotice();

  const saved = state.lastPromptInspector;
  const panel = el("section", "panel prompt-inspector-panel");
  const inputLabel = el("label", "control-label prompt-input-label");
  inputLabel.appendChild(el("span", "", t("context.userInput", {}, "用户输入")));
  const input = el("textarea");
  input.id = "prompt-inspector-input";
  input.placeholder = t(
    "context.inspectorPlaceholder",
    {},
    "输入要检查的鲸鱼记忆上下文查询",
  );
  input.value = saved.input || "";
  inputLabel.appendChild(input);

  const controlGrid = el("div", "prompt-control-grid");
  const summaryLabel = el("label", "control-label");
  summaryLabel.append(
    el("span", "", t("legacy.tools.context.summaryLimit", {}, "摘要数量上限")),
    numberInput("prompt-summary-limit", saved.summaryLimit, 1, 10),
  );
  const rawLabel = el("label", "control-label");
  rawLabel.append(
    el("span", "", t("legacy.tools.context.rawMemoryTopK", {}, "原始记忆返回数")),
    numberInput("prompt-raw-memory-top-k", saved.rawMemoryTopK, 1, 10),
  );
  const previewLabel = el("label", "control-label");
  previewLabel.append(
    el("span", "", t("legacy.tools.context.previewCharacters", {}, "预览字符数")),
    numberInput("prompt-preview-chars", saved.previewChars, 1, 1000),
  );
  const generalLabel = el("label", "control-label checkbox-label");
  generalLabel.append(
    checkboxInput("prompt-include-general", saved.includeGeneral),
    el("span", "", t("legacy.tools.context.includeGeneral", {}, "纳入通用上下文")),
  );
  const fullLabel = el("label", "control-label checkbox-label");
  fullLabel.append(
    checkboxInput("prompt-show-full", saved.showFullPrompt),
    el("span", "", t("legacy.tools.context.showFullPrompt", {}, "显示完整提示词")),
  );
  controlGrid.append(summaryLabel, rawLabel, previewLabel, generalLabel, fullLabel);

  const actions = el("div", "actions");
  const previewButton = button(t("context.preview", {}, "预览上下文"));
  previewButton.id = "prompt-preview-button";
  actions.appendChild(previewButton);
  const message = el(
    "p",
    "form-message",
    t(
      "context.inspectorDescription",
      {},
      "只读检查鲸鱼记忆 Prompt 上下文；不会调用模型或写入数据库。",
    ),
  );
  message.id = "prompt-inspector-message";
  panel.append(inputLabel, controlGrid, actions, message);

  const results = el("section", "content");
  results.id = "prompt-inspector-results";
  content.append(panel, results);

  previewButton.addEventListener("click", runPromptInspector);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) runPromptInspector();
  });
  if (saved.data) renderPromptInspectorResults(saved.data);
  input.focus();
}

async function runPromptInspector() {
  const input = document.querySelector("#prompt-inspector-input");
  const message = document.querySelector("#prompt-inspector-message");
  const results = document.querySelector("#prompt-inspector-results");
  const query = input.value.trim();
  if (!query) {
    state.lastPromptInspector.input = "";
    state.lastPromptInspector.data = null;
    const required = t("context.userInputRequired", {}, "请输入用户输入。");
    renderEmpty(required, results);
    setMessage(message, required, "error");
    input.focus();
    return;
  }

  const nextState = {
    input: query,
    summaryLimit: document.querySelector("#prompt-summary-limit")?.value || "5",
    rawMemoryTopK: document.querySelector("#prompt-raw-memory-top-k")?.value || "5",
    includeGeneral: Boolean(document.querySelector("#prompt-include-general")?.checked),
    previewChars: document.querySelector("#prompt-preview-chars")?.value || "300",
    showFullPrompt: Boolean(document.querySelector("#prompt-show-full")?.checked),
    data: null,
  };
  const params = new URLSearchParams({
    input: nextState.input,
    summary_limit: nextState.summaryLimit,
    raw_memory_top_k: nextState.rawMemoryTopK,
    include_general: String(nextState.includeGeneral),
    preview_chars: nextState.previewChars,
    show_full_prompt: String(nextState.showFullPrompt),
  });

  setMessage(message, t("context.reading", {}, "正在读取上下文…"));
  results.replaceChildren(el(
    "div",
    "panel",
    t("context.reading", {}, "正在读取上下文…"),
  ));
  try {
    const data = await api(`/api/whale-prompt-preview?${params.toString()}`);
    state.lastPromptInspector = {
      ...nextState,
      summaryLimit: Number(nextState.summaryLimit),
      rawMemoryTopK: Number(nextState.rawMemoryTopK),
      previewChars: Number(nextState.previewChars),
      data,
    };
    renderPromptInspectorResults(data);
    setMessage(message, t("context.previewUpdated", {}, "上下文预览已更新。"));
  } catch (error) {
    state.lastPromptInspector = { ...state.lastPromptInspector, ...nextState, data: null };
    renderTechnicalError(error, results);
    setTechnicalMessage(message, error, "error.loadFailedRetry", "内容加载失败，请重试。");
  }
}

function renderPromptInspectorResults(data) {
  const results = document.querySelector("#prompt-inspector-results");
  if (!results) return;
  results.innerHTML = "";
  const totals = data.totals || {};
  const ordering = data.ordering || {};

  const overview = el("section", "panel prompt-overview");
  const metrics = el("div", "summary-health-grid prompt-metrics");
  metrics.append(
    metricCard(
      t("legacy.tools.context.totalCharacters", {}, "总字符数"),
      formatUiNumber(totals.total_chars ?? 0),
    ),
    metricCard(
      t("legacy.tools.context.estimatedTotalTokens", {}, "预计 Token 总数"),
      formatUiNumber(totals.estimated_total_tokens ?? 0),
    ),
    metricCard(
      t("legacy.tools.context.enabledSections", {}, "已启用区段"),
      formatUiNumber(totals.enabled_sections ?? 0),
    ),
    metricCard(
      t("legacy.tools.context.summaryCount", {}, "摘要数"),
      formatUiNumber(totals.summary_count ?? 0),
    ),
    metricCard(
      t("legacy.tools.context.rawMemoryCount", {}, "原始记忆数"),
      formatUiNumber(totals.raw_memory_count ?? 0),
    ),
    metricCard(
      t("legacy.tools.context.generalContextCount", {}, "通用上下文数"),
      formatUiNumber(totals.general_context_count ?? 0),
    ),
    metricCard(
      t("legacy.tools.context.summaryBeforeRawMemory", {}, "摘要先于原始记忆"),
      runtimeBoolean(Boolean(ordering.summary_before_raw_memory)),
    ),
  );
  overview.append(el("h3", "section-title", t("context.totals", {}, "总计")), metrics);
  const sectionOrder = el(
    "p",
    "reference-uri",
    (ordering.section_order || []).map(legacyPromptSectionLabel).join(" → "),
  );
  sectionOrder.setAttribute(
    "aria-label",
    t("legacy.tools.context.sectionOrder", {}, "区段顺序"),
  );
  overview.append(
    el("div", "label", t("legacy.tools.context.sectionOrder", {}, "区段顺序")),
    sectionOrder,
  );
  results.appendChild(overview);

  (data.sections || []).forEach((section) => {
    const card = el("article", "card prompt-section-card");
    const header = el("div", "result-top");
    const sectionLabel = legacyPromptSectionLabel(section.name);
    header.append(
      el("h3", "card-title", sectionLabel),
      el(
        "span",
        section.enabled ? "health-pass" : "health-badge muted",
        section.enabled
          ? t("context.enabled", {}, "已启用")
          : t("context.disabled", {}, "已禁用"),
      ),
    );
    const meta = el("div", "meta-grid");
    meta.append(
      metaItem(t("legacy.tools.context.nameLabel", {}, "名称："), sectionLabel),
      metaItem(
        t("legacy.tools.context.enabledLabel", {}, "已启用："),
        runtimeBoolean(Boolean(section.enabled)),
      ),
      metaItem(
        t("legacy.tools.context.itemCountLabel", {}, "条目数："),
        formatUiNumber(section.item_count ?? 0),
      ),
      metaItem(
        t("legacy.tools.context.charactersLabel", {}, "字符数："),
        formatUiNumber(section.chars ?? 0),
      ),
      metaItem(
        t("legacy.tools.context.estimatedTokensLabel", {}, "预计 Token："),
        formatUiNumber(section.estimated_tokens ?? 0),
      ),
    );
    const preview = el("pre", "prompt-preview-text", section.preview || "");
    card.append(
      header,
      meta,
      el("div", "label", t("legacy.tools.context.previewLabel", {}, "预览")),
      preview,
    );
    results.appendChild(card);
  });

  if (data.prompt) {
    const fullPanel = el("section", "panel");
    fullPanel.appendChild(el("h3", "section-title", t("context.fullPrompt", {}, "完整提示词")));
    const prompt = el("textarea", "prompt-full-text");
    prompt.value = data.prompt;
    prompt.readOnly = true;
    prompt.setAttribute(
      "aria-label",
      t("context.fullPrompt", {}, "完整提示词"),
    );
    fullPanel.appendChild(prompt);
    results.appendChild(fullPanel);
  }
}

function loadContextOptimizer() {
  setActive("contextOptimizer");
  setTitle(t("context.optimizer", {}, "上下文优化器"));
  content.innerHTML = "";
  appendReadonlyNotice();

  const saved = state.lastContextOptimizer;
  const panel = el("section", "panel context-optimizer-panel");
  const inputLabel = el("label", "control-label prompt-input-label");
  inputLabel.appendChild(el("span", "", t("context.userInput", {}, "用户输入")));
  const input = el("textarea");
  input.id = "context-optimizer-input";
  input.placeholder = t(
    "context.optimizerPlaceholder",
    {},
    "输入要分析的鲸鱼记忆上下文查询",
  );
  input.value = saved.input || "";
  inputLabel.appendChild(input);

  const controlGrid = el("div", "optimizer-control-grid");
  const generalLabel = el("label", "control-label checkbox-label");
  generalLabel.append(
    checkboxInput("context-test-general", saved.testGeneral),
    el("span", "", t("legacy.tools.context.testGeneral", {}, "测试通用上下文")),
  );
  const limitLabel = el("label", "control-label");
  limitLabel.append(
    el("span", "", t("legacy.tools.context.resultLimit", {}, "返回配置数")),
    numberInput("context-optimizer-limit", saved.limit, 1, 10),
  );
  const maxTokensLabel = el("label", "control-label");
  const maxTokens = el("input");
  maxTokens.id = "context-optimizer-max-tokens";
  maxTokens.type = "number";
  maxTokens.min = "1";
  maxTokens.max = "100000";
  maxTokens.placeholder = t("common.optional", {}, "可选");
  maxTokens.value = saved.maxTokens || "";
  maxTokensLabel.append(
    el("span", "", t("legacy.tools.context.maxTokens", {}, "最大 Token 数")),
    maxTokens,
  );
  controlGrid.append(generalLabel, limitLabel, maxTokensLabel);

  const actions = el("div", "actions");
  const analyzeButton = button(t("context.analyze", {}, "分析配置"));
  analyzeButton.id = "context-optimizer-button";
  actions.appendChild(analyzeButton);
  const message = el(
    "p",
    "form-message",
    t(
      "context.optimizerDescription",
      {},
      "只读比较 Prompt 参数组合；不会写入数据库、保存配置或调用模型。",
    ),
  );
  message.id = "context-optimizer-message";
  panel.append(inputLabel, controlGrid, actions, message);

  const results = el("section", "content");
  results.id = "context-optimizer-results";
  content.append(panel, results);

  analyzeButton.addEventListener("click", runContextOptimizer);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) runContextOptimizer();
  });
  if (saved.data) renderContextOptimizerResults(saved.data);
  input.focus();
}

async function runContextOptimizer() {
  const input = document.querySelector("#context-optimizer-input");
  const message = document.querySelector("#context-optimizer-message");
  const results = document.querySelector("#context-optimizer-results");
  const query = input.value.trim();
  if (!query) {
    state.lastContextOptimizer.input = "";
    state.lastContextOptimizer.data = null;
    const required = t("context.userInputRequired", {}, "请输入用户输入。");
    renderEmpty(required, results);
    setMessage(message, required, "error");
    input.focus();
    return;
  }

  const nextState = {
    input: query,
    testGeneral: Boolean(document.querySelector("#context-test-general")?.checked),
    limit: document.querySelector("#context-optimizer-limit")?.value || "5",
    maxTokens: document.querySelector("#context-optimizer-max-tokens")?.value || "",
    data: null,
  };
  const params = new URLSearchParams({
    input: nextState.input,
    test_general: String(nextState.testGeneral),
    limit: nextState.limit,
  });
  if (nextState.maxTokens) params.set("max_tokens", nextState.maxTokens);

  setMessage(message, t("context.analyzing", {}, "正在分析配置…"));
  results.replaceChildren(el(
    "div",
    "panel",
    t(
      "context.analyzing",
      {},
      "正在分析配置…",
    ),
  ));
  try {
    const data = await api(`/api/whale-context-optimizer?${params.toString()}`);
    state.lastContextOptimizer = {
      ...nextState,
      limit: Number(nextState.limit),
      data,
    };
    renderContextOptimizerResults(data);
    setMessage(message, t("context.analysisUpdated", {}, "配置分析已更新。"));
  } catch (error) {
    state.lastContextOptimizer = { ...state.lastContextOptimizer, ...nextState, data: null };
    renderTechnicalError(error, results);
    setTechnicalMessage(message, error, "error.loadFailedRetry", "内容加载失败，请重试。");
  }
}

function renderContextOptimizerResults(data) {
  const results = document.querySelector("#context-optimizer-results");
  if (!results) return;
  results.innerHTML = "";

  const recommended = data.recommended_config || {};
  const overview = el("section", "panel optimizer-overview");
  const metrics = el("div", "summary-health-grid prompt-metrics");
  metrics.append(
    metricCard(
      t("legacy.tools.context.candidateConfigCount", {}, "候选配置"),
      formatUiNumber(data.candidate_count ?? 0),
    ),
    metricCard(
      t("legacy.tools.common.readonly", {}, "只读"),
      runtimeBoolean(Boolean(data.readonly)),
    ),
    metricCard(
      t("legacy.tools.context.applied", {}, "已应用"),
      runtimeBoolean(Boolean(data.applied)),
    ),
    metricCard(
      t("runtime.sentToModel", {}, "已发送至模型"),
      runtimeBoolean(Boolean(data.sent_to_model)),
    ),
  );
  const config = el("div", "optimizer-recommended");
  config.append(
    el("h3", "section-title", t("context.recommendedConfig", {}, "推荐配置")),
    optimizerConfigLine(
      t("legacy.tools.context.summaryLimit", {}, "摘要数量上限"),
      recommended.summary_limit,
    ),
    optimizerConfigLine(
      t("legacy.tools.context.rawMemoryTopK", {}, "原始记忆返回数"),
      recommended.raw_memory_top_k,
    ),
    optimizerConfigLine(
      t("legacy.tools.context.includeGeneral", {}, "纳入通用上下文"),
      runtimeBoolean(Boolean(recommended.include_general)),
    ),
  );
  const reason = el("p", "summary-text", legacyOptimizerReason(data.reason));
  overview.append(
    metrics,
    config,
    el("h3", "section-title", t("context.reason", {}, "原因")),
    reason,
  );
  results.appendChild(overview);

  const tablePanel = el("section", "panel optimizer-table-panel");
  tablePanel.appendChild(
    el("h3", "section-title", t("context.topConfigs", {}, "最佳配置")),
  );
  tablePanel.appendChild(contextOptimizerTable(data.top_configs || []));
  results.appendChild(tablePanel);
}

function optimizerConfigLine(label, value) {
  const row = el("div", "meta-item");
  row.append(
    el(
      "strong",
      "",
      t("legacy.tools.common.labelWithColon", { label }, "{label}："),
    ),
    document.createTextNode(String(value ?? "")),
  );
  return row;
}

function contextOptimizerTable(configs) {
  const table = el("table", "optimizer-table");
  const header = el("thead");
  const headerRow = el("tr");
  [
    t("legacy.tools.context.summaryLimit", {}, "摘要数量上限"),
    t("legacy.tools.context.rawMemoryTopK", {}, "原始记忆返回数"),
    t("legacy.tools.context.includeGeneral", {}, "纳入通用上下文"),
    t("legacy.tools.context.estimatedTotalTokens", {}, "预计 Token 总数"),
    t("legacy.tools.context.summaryCount", {}, "摘要数"),
    t("legacy.tools.context.rawMemoryCount", {}, "原始记忆数"),
    t("legacy.tools.context.generalContextCount", {}, "通用上下文数"),
    t("legacy.tools.context.score", {}, "评分"),
  ].forEach((label) => headerRow.appendChild(el("th", "", label)));
  header.appendChild(headerRow);
  const body = el("tbody");
  configs.forEach((config) => {
    const row = el("tr");
    [
      formatUiNumber(config.summary_limit ?? 0),
      formatUiNumber(config.raw_memory_top_k ?? 0),
      runtimeBoolean(Boolean(config.include_general)),
      formatUiNumber(config.estimated_total_tokens ?? 0),
      formatUiNumber(config.summary_count ?? 0),
      formatUiNumber(config.raw_memory_count ?? 0),
      formatUiNumber(config.general_context_count ?? 0),
      formatUiNumber(config.score ?? 0),
    ].forEach((value) => row.appendChild(el("td", "", String(value ?? ""))));
    body.appendChild(row);
  });
  table.append(header, body);
  return table;
}

const OPERATIONAL_VIEWS = Object.freeze({
  operationalNow: ["nav.now", "当前"],
  operationalProjects: ["nav.projects", "项目"],
  operationalWaiting: ["nav.waiting", "等待"],
  operationalDecisions: ["nav.decisions", "决策"],
  operationalDelegations: ["nav.delegations", "委托"],
});
const operationalReadCache = new Map();

function operationalText(value, fallback = t("common.notRecorded", {}, "未记录")) {
  if (value === null || value === undefined || value === "") return fallback;
  if (Array.isArray(value)) return value.length ? value.join(" · ") : fallback;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function operationalLabel(item = {}) {
  const fields = item.fields || {};
  return operationalText(
    fields.title || fields.name || fields.summary || fields.description || item.entity_id,
  );
}

function operationalMeta(parts) {
  return parts.filter(Boolean).join(" · ");
}

function formatOperationalTime(value) {
  if (!value) return t("date.timeNotRecorded", {}, "时间未记录");
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return formatUiDateTime(parsed, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function operationalDuration(value) {
  const started = new Date(value).getTime();
  if (!value || Number.isNaN(started)) {
    return t("date.durationNotRecorded", {}, "时长未记录");
  }
  const duration = formatUiDuration(Math.max(0, Date.now() - started));
  return t("units.runtime", { duration }, `运行了${duration}`);
}

async function readOperationalApi(path) {
  try {
    const data = await api(path);
    const cachedAt = new Date().toISOString();
    operationalReadCache.set(path, { data, cachedAt });
    return { data, live: true, cachedAt };
  } catch (error) {
    const cached = operationalReadCache.get(path);
    if (!cached || !allowsPrivateCacheFallback(error)) throw error;
    return { data: cached.data, live: false, cachedAt: cached.cachedAt };
  }
}

function buildOperationalStateBar(result) {
  const bar = el("section", "yunos-operational-state-bar");
  bar.append(
    el(
      "strong",
      result.live ? "is-live" : "is-cached",
      result.live
        ? t("operational.livePrivateReadonly", {}, "实时 · 私有 · 只读")
        : t("operational.cachedStaleReadonly", {}, "缓存 · 状态陈旧 · 只读"),
    ),
    el(
      "span",
      "",
      result.live
        ? t(
          "operational.liveNoStore",
          {},
          "实时读取 · Cache-Control: no-store",
        )
        : t(
          "operational.cachedAt",
          { time: formatOperationalTime(result.cachedAt) },
          `缓存于 ${formatOperationalTime(result.cachedAt)}；内容可能已过期`,
        ),
    ),
  );
  return bar;
}

function buildOperationalEmpty(message = t("operational.empty", {}, "暂无条目")) {
  return el("p", "yunos-operational-empty", message);
}

function buildOperationalProjection(item, { compact = false } = {}) {
  const row = el("article", `yunos-operational-row${compact ? " is-compact" : ""}`);
  const copy = el("div", "yunos-operational-row-copy");
  copy.append(
    el("h3", "", operationalLabel(item)),
    el(
      "p",
      "yunos-operational-meta",
      operationalMeta([
        enumLabel("entity", item.entity_type),
        enumLabel("status", item.state),
        enumLabel("freshness", item.freshness),
        item.project_id,
      ]),
    ),
  );
  row.appendChild(copy);
  if (item.trust) {
    row.appendChild(el(
      "span",
      "yunos-operational-chip",
      enumLabel("trust", item.trust),
    ));
  }
  return row;
}

function buildOperationalSection(titleText, items, {
  empty = t("operational.empty", {}, "暂无条目"),
  renderItem = (item) => buildOperationalProjection(item),
  className = "",
} = {}) {
  const section = el("section", `yunos-operational-section ${className}`.trim());
  const header = el("header", "yunos-operational-section-header");
  header.append(
    el("h2", "", titleText),
    el("span", "yunos-operational-count", formatUiNumber(items.length)),
  );
  section.appendChild(header);
  if (!items.length) {
    section.appendChild(buildOperationalEmpty(empty));
    return section;
  }
  const list = el("div", "yunos-operational-list");
  items.forEach((item) => list.appendChild(renderItem(item)));
  section.appendChild(list);
  return section;
}

function operationalUnavailable(statusResult, target) {
  target.replaceChildren(buildOperationalStateBar(statusResult));
  const card = el("section", "yunos-operational-unavailable");
  card.append(
    el("p", "yunos-operational-kicker", t("operational.title", {}, "运行状态")),
    el(
      "h2",
      "",
      t("operational.databaseDisabled", {}, "运行状态数据库尚未启用。"),
    ),
    el(
      "p",
      "",
      t(
        "operational.databaseDisabledBody",
        {},
        "只读入口保持可用，但没有可展示的生产状态。系统没有创建、写入或推断任何状态。",
      ),
    ),
    el("code", "", operationalText(statusResult.data.reason_code)),
  );
  target.appendChild(card);
}

async function operationalStatus(target) {
  const result = await readOperationalApi("/api/operational-state/status");
  if (result.data.status !== "ready") {
    operationalUnavailable(result, target);
    return null;
  }
  return result;
}

function appendOperationalHeader(target, result, eyebrow, heading, description) {
  target.replaceChildren(buildOperationalStateBar(result));
  const header = el("header", "yunos-operational-page-header");
  header.append(
    el("p", "yunos-operational-kicker", eyebrow),
    el("h1", "", heading),
    el("p", "", description),
  );
  target.appendChild(header);
}

function buildOperationalMetrics(entries) {
  const strip = el("section", "yunos-operational-metrics");
  strip.setAttribute(
    "aria-label",
    t("operational.summary", {}, "状态摘要"),
  );
  entries.forEach(([label, value]) => {
    const metric = el("div", "yunos-operational-metric");
    metric.append(
      el("strong", "", formatUiNumber(value)),
      el("span", "", label),
    );
    strip.appendChild(metric);
  });
  return strip;
}

function renderOperationalNow(result, target) {
  const data = result.data;
  appendOperationalHeader(
    target,
    result,
    t("operational.currentFirst", {}, "当前优先 · 有界上下文"),
    t("nav.now", {}, "当前"),
    t(
      "operational.nowDescription",
      {},
      "此刻需要注意的状态，不展开完整历史。",
    ),
  );
  target.appendChild(buildOperationalMetrics([
    [t("operational.projects", {}, "项目"), data.active_projects?.length || 0],
    [t("operational.goals", {}, "目标"), data.active_goals?.length || 0],
    [t("operational.blockers", {}, "阻塞项"), data.blockers?.length || 0],
    [t("operational.waiting", {}, "等待"), data.waiting?.length || 0],
    [t("operational.decisions", {}, "决策"), data.pending_decisions?.length || 0],
  ]));
  target.appendChild(buildOperationalSection(
    t("operational.focus", {}, "当前重点"),
    data.current_focus || [],
    {
    empty: t("operational.noFocus", {}, "当前没有明确重点。"),
    renderItem: (item) => {
      const row = el("article", "yunos-operational-focus");
      row.append(
        el(
          "p",
          "yunos-operational-kicker",
          item.selection_reason || t("operational.currentFocus", {}, "当前重点"),
        ),
        el("h2", "", operationalText(item.name || item.entity_id)),
        el(
          "p",
          "yunos-operational-meta",
          operationalMeta([
            enumLabel("status", item.state),
            enumLabel("freshness", item.freshness),
          ]),
        ),
      );
      return row;
    },
  }));
  const grid = el("div", "yunos-operational-grid");
  [
    [t("operational.activeProjects", {}, "进行中的项目"), data.active_projects || []],
    [t("operational.activeGoals", {}, "进行中的目标"), data.active_goals || []],
    [t("operational.blockers", {}, "阻塞项"), data.blockers || []],
    [t("operational.waiting", {}, "等待"), data.waiting || []],
    [t("operational.userDecisions", {}, "等待用户决策"), data.pending_decisions || []],
    [t("operational.stale", {}, "状态陈旧"), data.stale || []],
    [t("operational.conflicts", {}, "冲突"), data.conflicts || []],
    [t("operational.recentChanges", {}, "最近变化"), data.recent_changes || []],
  ].forEach(([sectionTitle, items]) => {
    grid.appendChild(buildOperationalSection(sectionTitle, items, {
      renderItem: (item) => buildOperationalProjection(item, { compact: true }),
    }));
  });
  target.appendChild(grid);
}

function buildOperationalProjectButton(item, target, mobile) {
  const button = el("button", "yunos-operational-project", "");
  button.type = "button";
  button.append(
    el("span", "yunos-operational-project-name", operationalLabel(item)),
    el(
      "span",
      "yunos-operational-meta",
      operationalMeta([
        item.fields?.current_phase,
        item.fields?.current_version,
        enumLabel("status", item.state),
        enumLabel("freshness", item.freshness),
      ]),
    ),
    el(
      "span",
      "yunos-operational-project-action",
      t("operational.openResume", {}, "打开接力包 →"),
    ),
  );
  button.addEventListener("click", () => {
    loadOperationalView("operationalProjects", { target, mobile, projectId: item.entity_id })
      .catch((error) => renderOperationalError(target, error));
  });
  return button;
}

function buildOperationalDetailList(titleText, values) {
  const normalized = Array.isArray(values) ? values : [];
  return buildOperationalSection(titleText, normalized, {
    renderItem: (item) => (
      typeof item === "object"
        ? buildOperationalProjection(item, { compact: true })
        : el("p", "yunos-operational-line", operationalText(item))
    ),
  });
}

function buildOperationalSourceDetails(titleText, items, renderer) {
  const details = el("details", "yunos-operational-details");
  const body = el("div", "yunos-operational-details-body");
  if (!items.length) body.appendChild(buildOperationalEmpty());
  items.forEach((item) => body.appendChild(renderer(item)));
  const count = formatUiNumber(items.length);
  details.append(
    el(
      "summary",
      "",
      t(
        "operational.extra.sectionWithCount",
        { title: titleText, count },
        `${titleText}（${count}）`,
      ),
    ),
    body,
  );
  return details;
}

function renderOperationalResume(result, target, mobile) {
  const data = result.data;
  appendOperationalHeader(
    target,
    result,
    t("operational.projectResumeSourceBacked", {}, "项目接力 · 来源可追溯"),
    operationalText(data.project_id),
    operationalText(
      data.project_purpose,
      t("operational.projectPurposeMissing", {}, "项目目的未记录。"),
    ),
  );
  const back = el(
    "button",
    "yunos-operational-back",
    t("operational.backToProjects", {}, "← 返回项目"),
  );
  back.type = "button";
  back.addEventListener("click", () => {
    loadOperationalView("operationalProjects", { target, mobile })
      .catch((error) => renderOperationalError(target, error));
  });
  target.querySelector(".yunos-operational-page-header")?.prepend(back);
  const facts = el("dl", "yunos-operational-facts");
  [
    [t("operational.authorityBaseline", {}, "权威基线"), data.authoritative_baseline],
    [t("operational.version", {}, "版本"), data.current_version],
    [t("operational.currentPhase", {}, "当前阶段"), data.current_phase],
    [t("operational.completedNodes", {}, "已完成阶段"), data.completed_nodes],
    [t("operational.nextApproved", {}, "下一批准步骤"), data.next_approved_step],
    [t("operational.pendingConflicts", {}, "待处理冲突"), data.pending_conflicts],
    [
      t("operational.futureNodesAuthorized", {}, "未来节点已授权"),
      data.future_nodes_authorized === false
        ? t("common.no", {}, "否")
        : data.future_nodes_authorized,
    ],
    [t("operational.authoritativeMain", {}, "权威 main"), data.authoritative_main],
    [t("operational.authoritativeTag", {}, "权威 tag"), data.authoritative_tag],
    [
      t("operational.state", {}, "状态"),
      enumLabel("status", data.project_state, "project"),
    ],
    [
      t("operational.freshness", {}, "新鲜度"),
      enumLabel("freshness", data.project_freshness),
    ],
  ].forEach(([term, value]) => {
    facts.append(el("dt", "", term), el("dd", "", operationalText(value)));
  });
  target.appendChild(facts);
  const grid = el("div", "yunos-operational-grid");
  [
    [t("operational.completed", {}, "已完成"), data.completed],
    [t("operational.currentWork", {}, "当前工作"), data.current_work],
    [t("operational.nextApproved", {}, "下一批准步骤"), data.next_approved_steps],
    [t("operational.nextCandidates", {}, "下一候选步骤"), data.next_candidate_steps],
    [t("operational.approvedDecisions", {}, "已批准决策"), data.approved_decisions],
    [t("operational.pendingDecisions", {}, "待决策"), data.pending_decisions],
    [t("operational.blockers", {}, "阻塞项"), data.blockers],
    [t("operational.waiting", {}, "等待"), data.waiting],
    [t("operational.delegation", {}, "委托"), data.delegations],
    [t("operational.outcome", {}, "执行结果"), data.outcomes],
    [t("operational.freshness", {}, "新鲜度"), data.freshness],
  ].forEach(([sectionTitle, items]) => {
    grid.appendChild(buildOperationalDetailList(sectionTitle, items || []));
  });
  target.appendChild(grid);
  target.append(
    buildOperationalDetailList(
      t("operational.deferred", {}, "已延期"),
      data.deferred || [],
    ),
    buildOperationalDetailList(
      t("operational.outOfScope", {}, "不在范围内"),
      data.out_of_scope || [],
    ),
    buildOperationalDetailList(
      t("operational.prohibited", {}, "禁止操作"),
      data.prohibited_actions || [],
    ),
    buildOperationalSourceDetails(
      t("operational.stateTimeline", {}, "状态时间轴"),
      data.state_timeline || [],
      (item) => {
        const row = el("article", "yunos-operational-timeline-row");
        row.append(
          el("strong", "", enumLabel("event", item.event_type)),
          el("span", "", operationalText(item.entity_id)),
          el("time", "", formatOperationalTime(item.effective_at)),
        );
        return row;
      },
    ),
    buildOperationalSourceDetails(
      t("operational.sourceAnchors", {}, "来源锚点"),
      data.source_anchors || [],
      (item) => {
        const row = el("article", "yunos-operational-source-row");
        row.append(
          el("strong", "", operationalText(item.source_kind || item.anchor_id)),
          el("span", "", operationalText(item.locator || item.anchor_id)),
          el("code", "", operationalText(item.evidence_digest || item.anchor_id)),
        );
        return row;
      },
    ),
  );
}

function renderOperationalProjects(result, target, mobile) {
  appendOperationalHeader(
    target,
    result,
    t("operational.resumeWithoutReconstruction", {}, "无需重建即可接力"),
    t("nav.projects", {}, "项目"),
    t(
      "operational.projectsDescription",
      {},
      "恢复目的、权威基线、当前阶段、下一步与来源锚点。",
    ),
  );
  const items = result.data.items || [];
  const list = el("div", "yunos-operational-projects");
  if (!items.length) {
    list.appendChild(buildOperationalEmpty(
      t("operational.noProjects", {}, "暂无项目。"),
    ));
  }
  items.forEach((item) => list.appendChild(buildOperationalProjectButton(item, target, mobile)));
  target.appendChild(list);
}

function waitingCategory(item) {
  const fields = item.fields || {};
  const dueAt = fields.due_at || fields.expires_at;
  if (dueAt && new Date(dueAt).getTime() < Date.now()) return "overdue";
  if (["stale", "expired", "unverifiable"].includes(item.freshness)) return "stale";
  const waitingFor = String(fields.waiting_for || fields.owner_kind || "").toLowerCase();
  if (waitingFor.includes("user")) return "user";
  if (waitingFor.includes("agent")) return "agent";
  if (waitingFor.includes("time") || dueAt) return "time";
  return "external";
}

function renderOperationalWaiting(result, target) {
  appendOperationalHeader(
    target,
    result,
    t("operational.explicitDependencyState", {}, "显式依赖状态"),
    t("nav.waiting", {}, "等待"),
    t(
      "operational.waitingDescription",
      {},
      "按等待用户、Agent、外部条件、时间条件、已超时与状态陈旧分类。",
    ),
  );
  const categories = [
    ["user", t("operational.waitingUser", {}, "等待用户")],
    ["agent", t("operational.waitingAgent", {}, "等待 Agent")],
    ["external", t("operational.waitingExternal", {}, "等待外部条件")],
    ["time", t("operational.waitingTime", {}, "等待时间条件")],
    ["overdue", t("operational.overdue", {}, "已超时")],
    ["stale", t("operational.stale", {}, "状态陈旧")],
  ];
  const grouped = Object.fromEntries(categories.map(([name]) => [name, []]));
  (result.data.items || []).forEach((item) => grouped[waitingCategory(item)].push(item));
  const grid = el("div", "yunos-operational-grid");
  categories.forEach(([name, label]) => {
    grid.appendChild(buildOperationalSection(label, grouped[name]));
  });
  target.appendChild(grid);
}

function renderOperationalDecisions(result, target) {
  appendOperationalHeader(
    target,
    result,
    t("operational.decisionLifecycle", {}, "决策生命周期"),
    t("nav.decisions", {}, "决策"),
    t(
      "operational.decisionsDescription",
      {},
      "候选不是事实；每个状态保持显式可见。",
    ),
  );
  const states = ["candidate", "approved", "rejected", "superseded", "revoked"];
  const grouped = Object.fromEntries(states.map((name) => [name, []]));
  (result.data.items || []).forEach((item) => {
    if (grouped[item.state]) grouped[item.state].push(item);
  });
  const grid = el("div", "yunos-operational-grid");
  states.forEach((name) => grid.appendChild(buildOperationalSection(
    enumLabel("status", name, "decision"),
    grouped[name],
  )));
  target.appendChild(grid);
}

function buildDelegationRow(item) {
  const delegation = item.delegation || {};
  const boundary = item.authority_boundary;
  const expired = boundary?.expires_at
    ? new Date(boundary.expires_at).getTime() < Date.now()
    : false;
  const row = el("article", "yunos-operational-delegation");
  const header = el("header", "");
  header.append(
    el("h3", "", operationalLabel(delegation)),
    el(
      "span",
      "yunos-operational-chip",
      enumLabel("status", operationalText(delegation.state)),
    ),
  );
  row.append(
    header,
    el(
      "p",
      "yunos-operational-meta",
      operationalMeta([
        t(
          "delegation.agentValue",
          {
            agent: operationalText(
              item.agent,
              t("common.notRecorded", {}, "未记录"),
            ),
          },
          `Agent ${operationalText(item.agent, "未记录")}`,
        ),
        operationalDuration(boundary?.valid_from),
        t(
          "delegation.activityValue",
          { sequence: operationalText(item.latest_activity_sequence) },
          `活动 #${operationalText(item.latest_activity_sequence)}`,
        ),
        boundary?.expires_at
          ? t(
            expired ? "delegation.timedOutAt" : "delegation.timeoutAt",
            { time: formatOperationalTime(boundary.expires_at) },
            `${expired ? "已超时" : "超时"} ${formatOperationalTime(boundary.expires_at)}`,
          )
          : t("delegation.timeoutNotRecorded", {}, "超时未记录"),
      ]),
    ),
  );
  const outcome = item.latest_outcome;
  const flags = el("div", "yunos-operational-flags");
  flags.append(
    el(
      "span",
      item.overreach ? "is-alert" : "",
      t(
        "delegation.overreachValue",
        { value: item.overreach ? t("common.yes", {}, "是") : t("common.no", {}, "否") },
        `越权 ${item.overreach ? "是" : "否"}`,
      ),
    ),
    el(
      "span",
      outcome?.verified ? "is-ok" : "",
      t(
        "delegation.verifiedValue",
        { value: outcome?.verified ? t("common.yes", {}, "是") : t("common.no", {}, "否") },
        `已验证 ${outcome?.verified ? "是" : "否"}`,
      ),
    ),
    el(
      "span",
      outcome?.accepted ? "is-ok" : "",
      t(
        "delegation.acceptedValue",
        { value: outcome?.accepted ? t("common.yes", {}, "是") : t("common.no", {}, "否") },
        `已接受 ${outcome?.accepted ? "是" : "否"}`,
      ),
    ),
  );
  row.appendChild(flags);
  const details = el("details", "yunos-operational-details");
  details.appendChild(el(
    "summary",
    "",
    t("delegation.authorityBoundaries", {}, "授权边界"),
  ));
  const body = el("dl", "yunos-operational-authority");
  [
    [t("delegation.readScope", {}, "可读取范围"), boundary?.read_scope],
    [t("delegation.writeScope", {}, "可修改范围"), boundary?.write_scope],
    [t("delegation.tools", {}, "工具"), boundary?.tool_scope],
    [t("delegation.prohibitedOperations", {}, "禁止操作"), boundary?.prohibited_actions],
    [t("operational.version", {}, "版本"), boundary?.version_scope],
    [t("delegation.expires", {}, "到期时间"), boundary?.expires_at],
    [t("delegation.risk", {}, "风险"), boundary?.risk],
    [t("delegation.commit", {}, "Commit"), boundary?.commit_permission],
    [t("delegation.merge", {}, "Merge"), boundary?.merge_permission],
    [t("delegation.tag", {}, "Tag"), boundary?.tag_permission],
    [t("delegation.push", {}, "Push"), boundary?.push_permission],
    [t("delegation.nextNode", {}, "下一节点"), boundary?.next_node_permission],
  ].forEach(([term, value]) => {
    body.append(el("dt", "", term), el("dd", "", operationalText(value)));
  });
  details.appendChild(body);
  row.appendChild(details);
  return row;
}

function renderOperationalDelegations(result, target) {
  appendOperationalHeader(
    target,
    result,
    t("delegation.authorityBoundExecution", {}, "授权边界内执行"),
    t("nav.delegations", {}, "委托"),
    t(
      "delegation.description",
      {},
      "Agent、权限边界、活动、超时、越权与验收结果。",
    ),
  );
  target.appendChild(buildOperationalSection(
    t("delegation.activeAndRecent", {}, "进行中与最近记录"),
    result.data.items || [],
    {
    renderItem: buildDelegationRow,
  }));
}

function renderOperationalError(target, error) {
  target.replaceChildren();
  const card = el("section", "yunos-operational-unavailable");
  card.setAttribute("role", "alert");
  const technicalDetails = document.createElement("details");
  technicalDetails.className = "yunos-operational-details";
  technicalDetails.append(
    el("summary", "", t("common.technicalDetails", {}, "技术详情")),
    el(
      "code",
      "",
      error?.technicalDetail
        || error?.message
        || "OPERATIONAL_STATE_UNAVAILABLE",
    ),
  );
  card.append(
    el(
      "p",
      "yunos-operational-kicker",
      t("operational.readonlySurface", {}, "只读界面"),
    ),
    el("h2", "", t("operational.unavailable", {}, "运行状态当前不可用。")),
    el(
      "p",
      "",
      error?.isNetworkError
        ? t("error.hostUnavailable", {}, "无法连接 YunOS 主机")
        : t("error.operationalStateReadFailed", {}, "无法读取运行状态。"),
    ),
    technicalDetails,
    el(
      "p",
      "yunos-operational-meta",
      t(
        "operational.noSyntheticCache",
        {},
        "未用缓存冒充实时状态，也未执行任何写入。",
      ),
    ),
  );
  target.appendChild(card);
}

async function loadOperationalView(view, {
  target = content,
  mobile = false,
  projectId = "",
} = {}) {
  const labelEntry = OPERATIONAL_VIEWS[view] || OPERATIONAL_VIEWS.operationalNow;
  const label = t(labelEntry[0], {}, labelEntry[1]);
  if (!mobile) {
    setTitle(label);
    setActive(view);
  } else if (mobileOperationalTitle) {
    mobileOperationalTitle.textContent = label;
    mobileOperationalNav?.querySelectorAll("[data-operational-view]").forEach((button) => {
      const active = button.dataset.operationalView === view;
      button.classList.toggle("is-active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  }
  target.replaceChildren(el(
    "p",
    "yunos-operational-loading",
    t("operational.loading", {}, "正在读取只读状态…"),
  ));
  const status = await operationalStatus(target);
  if (!status) return;
  let result;
  if (view === "operationalProjects" && projectId) {
    result = await readOperationalApi(
      `/api/operational-state/resume?project_id=${encodeURIComponent(projectId)}&budget=Medium`,
    );
    renderOperationalResume(result, target, mobile);
  } else if (view === "operationalProjects") {
    result = await readOperationalApi("/api/operational-state/projects");
    renderOperationalProjects(result, target, mobile);
  } else if (view === "operationalWaiting") {
    result = await readOperationalApi("/api/operational-state/waiting");
    renderOperationalWaiting(result, target);
  } else if (view === "operationalDecisions") {
    result = await readOperationalApi("/api/operational-state/decisions");
    renderOperationalDecisions(result, target);
  } else if (view === "operationalDelegations") {
    result = await readOperationalApi("/api/operational-state/delegations");
    renderOperationalDelegations(result, target);
  } else {
    result = await readOperationalApi("/api/operational-state/now?budget=Medium");
    renderOperationalNow(result, target);
  }
}

const views = {
  recent: loadRecent,
  search: () => renderSearch("", false),
  projects: loadProjects,
  timeline: () => loadTimeline({ groupBy: "month", project: "", q: "" }),
  summaries: () => loadSummaries({ memorySpace: "whale", summaryType: "" }),
  runtimeStatus: loadRuntimeStatus,
  runtimeExecution: loadRuntimeExecution,
  contextReview: loadContextReview,
  approvedProfiles: loadApprovedProfiles,
  promptInspector: loadPromptInspector,
  contextOptimizer: loadContextOptimizer,
  marks: loadMarks,
  tags: loadTags,
  about: loadAbout,
  operationalNow: () => loadOperationalView("operationalNow")
    .catch((error) => renderOperationalError(content, error)),
  operationalProjects: () => loadOperationalView("operationalProjects")
    .catch((error) => renderOperationalError(content, error)),
  operationalWaiting: () => loadOperationalView("operationalWaiting")
    .catch((error) => renderOperationalError(content, error)),
  operationalDecisions: () => loadOperationalView("operationalDecisions")
    .catch((error) => renderOperationalError(content, error)),
  operationalDelegations: () => loadOperationalView("operationalDelegations")
    .catch((error) => renderOperationalError(content, error)),
};

mobileOperationalButton?.addEventListener("click", () => {
  const currentView = mobileShell?.dataset.mobileView || "home";
  if (currentView === "operational") {
    setMobileView(mobileOperationalPreviousView);
    return;
  }
  mobileOperationalPreviousView = ["home", "results"].includes(currentView)
    ? currentView
    : "home";
  closeMobileRecentDrawer({ restoreFocus: false });
  setMobileView("operational");
  loadOperationalView("operationalNow", {
    target: mobileOperationalContent,
    mobile: true,
  }).catch((error) => renderOperationalError(mobileOperationalContent, error));
  mobileOperationalTitle?.focus({ preventScroll: true });
});

mobileOperationalClose?.addEventListener("click", () => {
  setMobileView(mobileOperationalPreviousView);
  mobileOperationalButton?.focus({ preventScroll: true });
});

mobileCognitiveButton?.addEventListener("click", () => {
  if (mobileShell?.dataset.mobileView === "cognitive") {
    mobileCognitiveRequestSerial += 1;
    setMobileView(mobileCognitivePreviousView);
    return;
  }
  void openMobileCognitiveWorkspace();
});

mobileCognitiveClose?.addEventListener("click", () => {
  mobileCognitiveRequestSerial += 1;
  setMobileView(mobileCognitivePreviousView);
  mobileCognitiveButton?.focus({ preventScroll: true });
});

mobileOperationalNav?.addEventListener("click", (event) => {
  const button = event.target instanceof Element
    ? event.target.closest("[data-operational-view]")
    : null;
  if (!(button instanceof HTMLButtonElement)) return;
  const view = button.dataset.operationalView;
  if (!view || !OPERATIONAL_VIEWS[view]) return;
  loadOperationalView(view, {
    target: mobileOperationalContent,
    mobile: true,
  }).catch((error) => renderOperationalError(mobileOperationalContent, error));
});

shellSidebarNav?.addEventListener("click", (event) => {
  const item = event.target instanceof Element ? event.target.closest("[data-view]") : null;
  if (!(item instanceof HTMLButtonElement)) return;
  const view = item.dataset.view;
  if (!view || typeof views[view] !== "function") return;
  setShellSidebarOpen(true, { trigger: "pinned" });
  setShellPageState("workspace");
  views[view]();
});

shellSidebarToggle?.addEventListener("click", () => {
  if (shellSidebarState.pinned) {
    setShellSidebarOpen(false, { trigger: "all" });
    return;
  }
  setShellSidebarOpen(true, { trigger: "pinned" });
});

shellSidebarEdge?.addEventListener("pointerenter", () => {
  shellSidebarState.pointerInside = true;
  scheduleShellSidebarHoverOpen();
});

shellSidebarEdge?.addEventListener("pointerleave", () => {
  shellSidebarState.pointerInside = false;
  scheduleShellSidebarHoverClose();
});

shellSidebar?.addEventListener("pointerenter", () => {
  shellSidebarState.pointerInside = true;
  clearShellSidebarTimer("closeTimer");
});

shellSidebar?.addEventListener("pointerleave", () => {
  shellSidebarState.pointerInside = false;
  scheduleShellSidebarHoverClose();
});

shellSidebar?.addEventListener("focusout", () => {
  window.setTimeout(() => {
    if (!shellSidebarState.pointerInside && !shellSidebar?.contains(document.activeElement)) {
      scheduleShellSidebarHoverClose();
    }
  }, 0);
});

document.addEventListener("pointerdown", (event) => {
  if (
    desktopShell?.dataset.sidebarMode !== "overlay"
    || desktopShell.dataset.sidebarOpen !== "true"
    || shellSidebar?.contains(event.target)
    || shellSidebarToggle?.contains(event.target)
  ) return;
  clearShellSidebarTimer("openTimer");
  clearShellSidebarTimer("closeTimer");
  setShellSidebarOpen(false, { trigger: "all" });
});

document.addEventListener("keydown", (event) => {
  if (
    event.key === "Escape"
    && !event.defaultPrevented
    && desktopShell.dataset.sidebarOpen === "true"
  ) {
    event.preventDefault();
    clearShellSidebarTimer("openTimer");
    clearShellSidebarTimer("closeTimer");
    setShellSidebarOpen(false, { trigger: "all", returnFocus: true });
  }
});

const initialLegacyViewRequestSerial = legacyViewRequestSerial;
if (document.body.dataset.yunosOfflineShell === "true") {
  dbMode.textContent = t(
    "database.mode",
    { mode: t("error.hostUnavailable", {}, "YunOS 主机不可用") },
    "数据库模式：YunOS 主机不可用",
  );
} else {
  loadStatus()
    .then(() => (isMobileSearchSurfaceActive() ? undefined : restoreHomeAiSession()))
    .catch(() => {
      dbMode.textContent = t(
        "database.mode",
        { mode: t("status.unavailable", {}, "不可用") },
        "数据库模式：不可用",
      );
    })
    .finally(() => {
      if (isMobileSearchSurfaceActive()) return;
      if (legacyViewRequestSerial !== initialLegacyViewRequestSerial) return;
      loadRecent()
        .then(() => {
          if (desktopShell?.dataset.pageState !== "workspace") setActive("");
        })
        .catch((error) => {
          renderTechnicalError(error);
        });
    });
}
