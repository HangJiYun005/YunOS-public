async (page, harness = {}) => {
  const baseUrl = harness.baseUrl || "http://127.0.0.1:8782/";
  const cognitiveFixture = harness.cognitiveFixture;
  const requests = [];
  const consoleErrors = [];
  const pageErrors = [];
  let expectingOfflineFailure = false;
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  assert(cognitiveFixture, "a real disposable Cognitive Run fixture is required");
  assert(cognitiveFixture.agency_level === "act", "fixture must exercise act supervision");

  page.setDefaultTimeout(15000);
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin === new URL(baseUrl).origin) {
      requests.push({ method: request.method(), path: url.pathname });
    }
  });
  page.on("console", (message) => {
    const text = message.text();
    const expectedUnavailable = text.includes("Failed to load resource")
      && (text.includes("503") || text.includes("404"));
    const expectedOfflineFailure = expectingOfflineFailure
      && text.includes("Failed to load resource")
      && text.includes("net::ERR_FAILED");
    if (
      message.type() === "error"
      && !text.includes("favicon.ico")
      && !expectedUnavailable
      && !expectedOfflineFailure
    ) {
      consoleErrors.push(text);
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  const splash = page.locator("#splash");
  if (await splash.count() && await splash.isVisible()) await splash.click();
  const emptyHome = await page.evaluate(() => ({
    dynamicSections: document.querySelectorAll("[data-cognitive-section]").length,
    supervision: document.querySelectorAll("[data-cognitive-supervision]").length,
    aiDocumentHidden: document.querySelector("#home-ai-document")?.hidden === true,
  }));
  assert(emptyHome.dynamicSections === 0, "empty home fabricated cognitive document state");
  assert(emptyHome.supervision === 0, "empty home fabricated an authority/supervision state");
  assert(emptyHome.aiDocumentHidden, "empty home exposed a cognitive result document");

  const registration = await page.evaluate(async () => {
    if (!("serviceWorker" in navigator)) return { supported: false };
    const ready = await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
      return { supported: true, scope: ready.scope, needsReload: true };
    }
    return { supported: true, scope: ready.scope, needsReload: false };
  });
  assert(registration.supported, "service worker unsupported in acceptance browser");
  if (registration.needsReload) {
    await page.reload({ waitUntil: "domcontentloaded" });
    if (await splash.count() && await splash.isVisible()) await splash.click();
  }
  await page.waitForFunction(() => Boolean(navigator.serviceWorker.controller));

  const liveCognitive = await page.evaluate(async () => {
    const response = await fetch("/api/cognitive/objectives?limit=1", {
      cache: "no-store",
    });
    return {
      status: response.status,
      cacheControl: response.headers.get("cache-control"),
      contentType: response.headers.get("content-type"),
    };
  });
  assert(
    liveCognitive.cacheControl === "no-store",
    `cognitive response is cacheable: ${JSON.stringify(liveCognitive)}`,
  );
  assert(
    (liveCognitive.contentType || "").includes("application/json"),
    "cognitive response is not JSON",
  );

  const cachedApiEntries = await page.evaluate(async () => {
    const entries = [];
    for (const cacheName of await caches.keys()) {
      const cache = await caches.open(cacheName);
      for (const request of await cache.keys()) {
        const url = new URL(request.url);
        if (url.pathname.startsWith("/api/")) {
          entries.push({ cacheName, path: url.pathname });
        }
      }
    }
    return entries;
  });
  assert(cachedApiEntries.length === 0, "Service Worker cached a private API response");

  const requestCountBeforeSearch = requests.length;
  const searchInput = page.locator("#mobile-search-input");
  await searchInput.fill("YunOS");
  const searchResponsePromise = page.waitForResponse((response) => (
    response.url().includes("/api/search?")
  ));
  await searchInput.press("Enter");
  const searchResponse = await searchResponsePromise;
  assert(searchResponse.ok(), "local Search API request failed");
  const searchRequests = requests.slice(requestCountBeforeSearch);
  assert(
    searchRequests.filter((request) => request.path === "/api/search").length === 1,
    "Search did not issue exactly one local search request",
  );
  assert(
    searchRequests.every((request) => (
      !request.path.startsWith("/api/runtime-")
      && !request.path.startsWith("/api/cognitive/")
    )),
    "Search called Runtime, Provider, or cognitive orchestration",
  );

  await page.locator("#mobile-cognitive-button").click();
  await page.locator(".yunos-mobile-cognitive-document").waitFor({ state: "visible" });
  const liveMobile = await page.evaluate(() => {
    const panel = document.querySelector("#mobile-cognitive-panel");
    const text = panel?.innerText || "";
    return {
      readonlyMarker: panel?.hasAttribute("data-cognitive-mobile-readonly") === true,
      hasDocument: Boolean(panel?.querySelector(".yunos-mobile-cognitive-document")),
      cardCount: panel?.querySelectorAll(".yunos-mobile-cognitive-card").length || 0,
      text,
      clientWidth: panel?.clientWidth || 0,
      scrollWidth: panel?.scrollWidth || 0,
    };
  });
  assert(liveMobile.readonlyMarker, "mobile cognitive readonly marker missing");
  assert(liveMobile.hasDocument, "mobile did not render the trusted Cognitive Run");
  assert(liveMobile.cardCount >= 7, "mobile omitted required readonly task sections");
  for (const label of ["目标理解", "计划", "进度", "Agent", "越权", "验证", "等待"]) {
    assert(liveMobile.text.includes(label), `mobile task omitted ${label}`);
  }
  assert(
    liveMobile.scrollWidth <= liveMobile.clientWidth + 1,
    `mobile cognitive panel overflow: ${liveMobile.scrollWidth}/${liveMobile.clientWidth}`,
  );
  await page.screenshot({
    path: `${harness.artifactDir}/mobile-readonly-workspace.png`,
    fullPage: true,
  });
  await page.evaluate(() => {
    const content = document.querySelector("#mobile-cognitive-content");
    if (content) content.scrollTop = content.scrollHeight;
  });
  await page.waitForTimeout(100);
  await page.screenshot({
    path: `${harness.artifactDir}/mobile-readonly-workspace-bottom.png`,
    fullPage: true,
  });

  await page.locator("#mobile-cognitive-close").click();

  expectingOfflineFailure = true;
  await page.context().setOffline(true);
  const offlineCognitive = await page.evaluate(async () => {
    try {
      const response = await fetch("/api/cognitive/objectives?limit=1", {
        cache: "no-store",
      });
      return { rejected: false, status: response.status };
    } catch (error) {
      return { rejected: true, name: error?.name || "Error" };
    }
  });
  assert(offlineCognitive.rejected, "offline cognitive GET masqueraded as live cached data");
  await page.locator("#mobile-cognitive-button").click();
  await page.locator(".yunos-mobile-cognitive-state.is-unavailable").waitFor({ state: "visible" });
  const offlineMobile = await page.evaluate(() => ({
    hasLiveDocument: Boolean(document.querySelector(".yunos-mobile-cognitive-document")),
    text: document.querySelector("#mobile-cognitive-panel")?.innerText || "",
  }));
  assert(!offlineMobile.hasLiveDocument, "offline mobile retained a live cognitive document");
  assert(
    offlineMobile.text.includes("不会使用缓存冒充实时状态"),
    "offline mobile did not disclose the no-cache boundary",
  );
  await page.context().setOffline(false);
  expectingOfflineFailure = false;
  await page.locator("#mobile-cognitive-close").click();

  const mobile = await page.evaluate(() => {
    const root = document.querySelector("[data-cognitive-mobile-readonly]");
    const forbidden = [
      "[data-cognitive-mutation]",
      "[data-cognitive-authorize]",
      "[data-cognitive-accept]",
      "[data-cognitive-write-tool]",
      "[data-cognitive-start-codex]",
    ].flatMap((selector) => Array.from(document.querySelectorAll(selector)))
      .filter((element) => {
        const style = getComputedStyle(element);
        return !element.hidden && style.display !== "none" && style.visibility !== "hidden";
      });
    return {
      markerCount: root ? 1 : 0,
      forbiddenVisibleCount: forbidden.length,
      htmlLang: document.documentElement.lang,
      viewportWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    };
  });
  assert(mobile.markerCount === 1, "mobile cognitive readonly marker missing");
  assert(mobile.forbiddenVisibleCount === 0, "mobile exposes a cognitive write control");
  assert(mobile.htmlLang === "zh-CN", `unexpected default locale: ${mobile.htmlLang}`);
  assert(
    mobile.scrollWidth <= mobile.viewportWidth + 1,
    `mobile horizontal overflow: ${mobile.scrollWidth}/${mobile.viewportWidth}`,
  );

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(100);
  const renderedDesktop = await page.evaluate(async () => {
    const bundle = await loadCognitiveWorkspaceBundle({
      surface: "desktop",
      allowLatest: true,
    });
    if (!bundle) throw new Error("trusted desktop Cognitive Run was not found");
    homeAiState.cognitiveBundle = bundle;
    homeAiState.cognitiveBundleStatus = "ready";
    homeAiState.cognitiveBundleDetail = "";
    homeAiState.phase = HOME_AI_PHASES.PREPARING_PREVIEW;
    homeAiState.preview = null;
    homeAiState.envelope = null;
    setHomeOmniboxMode("ai");
    const input = document.querySelector("#home-omnibox-input");
    if (input) input.value = "";
    setShellPageState("ai");
    document.querySelector("#home-ai-document").hidden = false;
    renderCognitiveWorkspace({
      phase: HOME_AI_PHASES.PREPARING_PREVIEW,
      lead: "Disposable authoritative Cognitive Run browser acceptance",
    });
    document.activeElement?.blur?.();
    const homeRegion = document.querySelector(".yunos-shell-home-region");
    if (homeRegion) homeRegion.scrollTop = 0;
    return { surface: bundle.surface, runId: bundle.run?.run_id };
  });
  assert(renderedDesktop.surface === "desktop", "desktop loaded the wrong surface contract");
  assert(renderedDesktop.runId === cognitiveFixture.run_id, "desktop rendered the wrong Cognitive Run");
  await page.locator("[data-cognitive-section]").first().waitFor({ state: "visible" });
  const desktop = await page.evaluate(() => ({
    workspaceCount: document.querySelectorAll("[data-cognitive-workspace]").length,
    supervisionCount: document.querySelectorAll("[data-cognitive-supervision]").length,
    sectionKeys: Array.from(document.querySelectorAll("[data-cognitive-section]"))
      .map((element) => element.dataset.cognitiveSection),
    viewportWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  assert(desktop.workspaceCount === 1, "desktop cognitive workspace marker missing");
  assert(desktop.supervisionCount === 1, "desktop supervision marker missing");
  assert(
    desktop.sectionKeys.join(",")
      === "objective,success,prohibited,context,plan,decisions,execution,verification,outcome,learning",
    `desktop cognitive section contract mismatch: ${desktop.sectionKeys.join(",")}`,
  );
  assert(
    desktop.scrollWidth <= desktop.viewportWidth + 1,
    `desktop horizontal overflow: ${desktop.scrollWidth}/${desktop.viewportWidth}`,
  );
  const mutationRequests = requests.filter((request) => (
    request.path.startsWith("/api/cognitive/") && request.method !== "GET"
  ));
  assert(mutationRequests.length === 0, "browser issued a cognitive mutation request");
  assert(consoleErrors.length === 0, `console errors: ${consoleErrors.join(" | ")}`);
  assert(pageErrors.length === 0, `page errors: ${pageErrors.join(" | ")}`);

  await page.screenshot({
    path: `${harness.artifactDir}/desktop-workspace-boundary.png`,
    fullPage: true,
  });
  return {
    liveCognitive,
    offlineCognitive,
    emptyHome,
    localSearch: {
      status: searchResponse.status(),
      searchRequests: 1,
      runtimeOrCognitiveRequests: 0,
    },
    cachedApiEntries,
    mobile: { ...mobile, live: liveMobile, offline: offlineMobile },
    desktop,
    cognitiveMutationRequests: mutationRequests.length,
    consoleErrors,
    pageErrors,
  };
}
