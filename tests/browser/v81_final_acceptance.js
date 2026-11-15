async (page) => {
  const fallbackBaseUrl = "http://127.0.0.1:8780/";
  const artifactDir = "E:/AyunOS/output/playwright/v8.1-f-final-acceptance";
  const fixedViewports = [
    { width: 2560, height: 1440 },
    { width: 1920, height: 1080 },
    { width: 1707, height: 1067 },
    { width: 1600, height: 900 },
    { width: 1440, height: 900 },
    { width: 1280, height: 720 },
    { width: 1100, height: 800 },
  ];
  const requiredViews = [
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
  ];
  const legacyReadyChecks = {
    runtimeStatus: { selector: "#content .runtime-status-panel", text: "D1 Production Runtime" },
    runtimeExecution: { selector: "#runtime-execution-question" },
    approvedProfiles: { selector: "#approved-profile-query" },
    recent: { selector: "#content .card.clickable, #content .empty-card" },
    search: { selector: "#search-input" },
    projects: { selector: "#project-source-platform" },
    timeline: { selector: "#timeline-group" },
    summaries: { selector: "#summary-memory-space" },
    contextReview: { selector: "#context-review-query" },
    promptInspector: { selector: "#prompt-inspector-input" },
    contextOptimizer: { selector: "#context-optimizer-input" },
    marks: { selector: "#content .card.clickable, #content .empty-card" },
    tags: { selector: "#content .tag-button, #content .empty-card" },
    about: { selector: "#content .section-title", text: "AyunOS 本地第二大脑原型" },
  };
  const consoleErrors = [];
  const pageErrors = [];
  const failedResponses = [];
  const externalRequests = [];
  const executeRequests = [];
  const screenshots = [];
  const fixedMeasurements = [];
  const continuousMeasurements = [];
  const legacyViews = [];
  const assert = (condition, message) => { if (!condition) throw new Error(message); };

  page.setDefaultTimeout(20000);
  const currentUrl = page.url();
  const baseUrl = currentUrl.startsWith("http://127.0.0.1:")
    ? `${currentUrl.match(/^https?:\/\/[^/]+/)[0]}/`
    : fallbackBaseUrl;

  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const location = message.location();
    if (location.url?.endsWith("/favicon.ico") && message.text().includes("404")) return;
    consoleErrors.push({ text: message.text(), location });
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) failedResponses.push({ status: response.status(), url: response.url() });
  });
  page.on("request", (request) => {
    const url = request.url();
    if (url.includes("/api/runtime-execute")) executeRequests.push(url);
    if (/^https?:/i.test(url) && !url.startsWith(baseUrl)) externalRequests.push(url);
  });

  const dismissCompatibilitySplash = async (target = page) => {
    const splash = target.locator("#splash");
    if (!(await splash.count())) return;
    assert(!(await splash.isVisible()), "compatibility splash became visible");
    await splash.waitFor({ state: "detached", timeout: 5000 });
  };
  const screenshot = async (name) => {
    const path = `${artifactDir}/${name}.png`;
    await page.screenshot({ path, fullPage: false });
    screenshots.push(path);
    return path;
  };
  const waitForAnimationFrames = () => page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  const waitForSidebarVisualState = async (open) => {
    await page.locator(`#yunos-desktop-shell[data-sidebar-open="${open}"]`).waitFor();
    await page.waitForFunction((expectedOpen) => {
      const sidebar = document.querySelector("#shell-sidebar");
      if (!sidebar) return false;
      const style = getComputedStyle(sidebar);
      const rect = sidebar.getBoundingClientRect();
      return expectedOpen
        ? style.visibility === "visible" && Math.abs(rect.left) <= 0.5
        : style.visibility === "hidden" && rect.right <= 1;
    }, open);
    await waitForAnimationFrames();
  };
  const waitForLegacyViewReady = async (view) => {
    const check = legacyReadyChecks[view];
    assert(check, `legacy ready check missing: ${view}`);
    const ready = page.locator(check.selector).first();
    await ready.waitFor({ state: "visible" });
    if (check.text) {
      await page.waitForFunction(
        ({ selector, expectedText }) => document.querySelector(selector)?.textContent?.includes(expectedText),
        { selector: check.selector, expectedText: check.text },
      );
    }
    assert(await ready.count() === 1, `legacy ready target missing: ${view}`);
  };
  const shellState = () => page.locator("#yunos-desktop-shell").evaluate((shell) => ({
    pageState: shell.dataset.pageState,
    sidebarMode: shell.dataset.sidebarMode,
    sidebarOpen: shell.dataset.sidebarOpen,
    sidebarPinned: shell.dataset.sidebarPinned,
  }));
  const homeGeometry = () => page.evaluate(() => {
    const shell = document.querySelector("#yunos-desktop-shell");
    const anchor = document.querySelector(".yunos-shell-omnibox-anchor").getBoundingClientRect();
    const sidebar = document.querySelector("#shell-sidebar").getBoundingClientRect();
    return {
      viewport: { width: innerWidth, height: innerHeight },
      anchor: { x: anchor.x, y: anchor.y, width: anchor.width, height: anchor.height },
      sidebarWidth: sidebar.width,
      shell: {
        pageState: shell.dataset.pageState,
        sidebarMode: shell.dataset.sidebarMode,
        sidebarOpen: shell.dataset.sidebarOpen,
      },
      horizontalOverflow:
        document.documentElement.scrollWidth > document.documentElement.clientWidth
        || document.body.scrollWidth > document.body.clientWidth,
    };
  });
  const expectedOmnibox = (width) => ({
    width: Math.min(864, Math.max(580, 0.28604924 * width + 131.713951)),
    height: Math.min(67.2, Math.max(52, 0.01547479 * width + 27.584525)),
  });
  const verifyHomeGeometry = (measurement) => {
    const expected = expectedOmnibox(measurement.viewport.width);
    assert(measurement.shell.pageState === "home", `home state lost at ${measurement.viewport.width}`);
    assert(measurement.shell.sidebarMode === "overlay", `home overlay lost at ${measurement.viewport.width}`);
    assert(!measurement.horizontalOverflow, `home overflow at ${measurement.viewport.width}`);
    assert(Math.abs(measurement.anchor.width - expected.width) <= 0.8, `Omnibox width drift at ${measurement.viewport.width}: ${measurement.anchor.width} != ${expected.width}`);
    assert(Math.abs(measurement.anchor.height - expected.height) <= 0.3, `Omnibox height drift at ${measurement.viewport.width}: ${measurement.anchor.height} != ${expected.height}`);
    assert(measurement.anchor.x >= 0 && measurement.anchor.x + measurement.anchor.width <= measurement.viewport.width + 1, `Omnibox clipped at ${measurement.viewport.width}`);
  };

  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await dismissCompatibilitySplash();
  await page.evaluate(() => {
    sessionStorage.removeItem("yunos.homeAiDocument.session.v1");
    localStorage.setItem("yunos.homeOmnibox.mode", "search");
    performance.clearResourceTimings();
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await dismissCompatibilitySplash();

  for (const viewport of fixedViewports) {
    await page.setViewportSize(viewport);
    await page.waitForTimeout(260);
    const before = await homeGeometry();
    verifyHomeGeometry(before);
    await page.locator("#shell-sidebar-toggle").click();
    await waitForSidebarVisualState(true);
    const after = await homeGeometry();
    for (const key of ["x", "y", "width", "height"]) {
      assert(Math.abs(after.anchor[key] - before.anchor[key]) <= 0.1, `overlay moved Omnibox ${key} at ${viewport.width}`);
    }
    assert(!after.horizontalOverflow, `overlay overflow at ${viewport.width}`);
    fixedMeasurements.push({ viewport, before, after });
    await page.locator("#shell-sidebar-toggle").click();
    await waitForSidebarVisualState(false);
    await screenshot(`home-default-${viewport.width}x${viewport.height}`);
  }

  const continuousWidths = Array.from({ length: 40 }, (_, index) => 1000 + (index * 40));
  [1000, 1100, 1279, 1280, 1359, 1360, 1707, 1799, 1800, 1920, 2560].forEach((width) => {
    if (!continuousWidths.includes(width)) continuousWidths.push(width);
  });
  continuousWidths.sort((left, right) => left - right);
  for (const width of continuousWidths) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(260);
    const measurement = await homeGeometry();
    verifyHomeGeometry(measurement);
    continuousMeasurements.push(measurement);
  }

  await page.setViewportSize({ width: 1707, height: 1067 });
  await page.locator("#home-omnibox-input").focus();
  const focusedStyle = await page.locator("#home-omnibox-form").evaluate((node) => {
    const style = getComputedStyle(node);
    return { borderColor: style.borderColor, boxShadow: style.boxShadow };
  });
  assert(focusedStyle.boxShadow !== "none", "Omnibox focus feedback missing");
  await screenshot("home-focused-1707x1067");
  const resourcesBeforeMode = await page.evaluate(() => performance.getEntriesByType("resource").length);
  await page.locator('[data-omnibox-mode="ai"]').click();
  assert(await page.locator('[data-omnibox-mode="ai"]').getAttribute("aria-pressed") === "true", "AI mode not selected");
  await page.locator('[data-omnibox-mode="search"]').click();
  const resourcesAfterMode = await page.evaluate(() => performance.getEntriesByType("resource").length);
  assert(resourcesAfterMode === resourcesBeforeMode, "mode switching emitted a request");

  await page.locator("#home-omnibox-input").fill("YunOS");
  const searchResponsePromise = page.waitForResponse((response) => response.url().includes("/api/search?"));
  await page.locator("#home-omnibox-input").press("Enter");
  const searchResponse = await searchResponsePromise;
  assert(searchResponse.ok(), "memory search failed");
  const searchPayload = await searchResponse.json();
  await page.locator("#home-memory-search-overlay").waitFor({ state: "visible" });
  const searchOptions = page.locator("#home-memory-search-results [role='option']");
  assert(await searchOptions.count() > 0, "memory search returned no results");
  await page.locator("#home-omnibox-input").press("ArrowDown");
  const activeResult = page.locator("#home-memory-search-results [aria-selected='true']");
  assert(await activeResult.count() === 1, "keyboard highlight missing");
  const activeResultId = await activeResult.getAttribute("id");
  const activeResultIndex = Number(String(activeResultId || "").split("-").pop());
  const selectedMessageId = searchPayload.items?.[activeResultIndex]?.message_id;
  assert(selectedMessageId, "selected search result omitted its message anchor");
  await screenshot("memory-search-keyboard-1707x1067");
  await page.locator("#home-omnibox-input").press("Enter");
  await page.locator('#yunos-desktop-shell[data-page-state="workspace"]').waitFor();
  const selectedMessage = page.locator(`.message[data-message-id="${selectedMessageId}"]`);
  await selectedMessage.waitFor({ state: "visible" });
  assert(await selectedMessage.evaluate((node) => node.classList.contains("selected-message")), "message anchor did not select source");
  assert(decodeURIComponent((page.url().split("#")[1] || "")) === selectedMessageId, "message anchor hash drifted");
  await page.getByRole("button", { name: "返回首页搜索" }).click();
  await page.locator('#yunos-desktop-shell[data-page-state="home"]').waitFor();
  await page.locator("#home-omnibox-input").press("Escape");

  await page.locator('[data-omnibox-mode="ai"]').click();
  await page.locator("#home-omnibox-input").fill("MacBook：三点偏好，一项不确定信息");
  const previewPromise = page.waitForResponse((response) => response.url().includes("/api/runtime-preview?"));
  await page.locator("#home-omnibox-input").press("Enter");
  const previewResponse = await previewPromise;
  const preview = await previewResponse.json();
  assert(previewResponse.ok() && preview.status === "preview", "AI review-only preview failed");
  assert(preview.execution?.credential_read === false, "preview read a credential");
  assert(preview.execution?.external_model_calls === false, "preview called a provider");
  assert(preview.execution?.network_request_count === 0, "preview crossed the network boundary");
  assert(!("payload" in (preview.request_artifact || {})), "review-only preview leaked the sealed Runtime payload");
  assert(preview.runtime_profile?.provider === "deepseek" && preview.runtime_profile?.model === "deepseek-v4-flash", "formal provider profile drifted");
  await page.locator('#home-ai-document[data-ai-state="preview-ready"]').waitFor({ state: "visible" });
  assert(await page.locator("#home-ai-execute").isDisabled(), "real-readonly preview exposed execution");
  await screenshot("ai-review-only-preview-1707x1067");
  await page.locator("#home-ai-cancel").click();
  await page.getByRole("button", { name: "返回首页", exact: true }).click();
  await page.locator('#yunos-desktop-shell[data-page-state="home"]').waitFor();

  await page.setViewportSize({ width: 1600, height: 900 });
  await page.locator("#shell-sidebar-toggle").click();
  await waitForSidebarVisualState(true);
  for (const selector of ["#sidebar-settings", "#sidebar-system-tools"]) {
    const details = page.locator(selector);
    if ((await details.getAttribute("open")) === null) await details.locator(":scope > summary").click();
  }
  for (const view of requiredViews) {
    const item = page.locator(`[data-view="${view}"]`);
    await item.scrollIntoViewIfNeeded();
    await item.click();
    await page.locator(`[data-view="${view}"][aria-current="page"]`).waitFor();
    await waitForSidebarVisualState(true);
    await waitForLegacyViewReady(view);
    const measurement = await page.evaluate(() => ({
      title: document.querySelector("#view-title")?.textContent || "",
      contentWidth: document.querySelector("#content")?.getBoundingClientRect().width || 0,
      horizontalOverflow:
        document.documentElement.scrollWidth > document.documentElement.clientWidth
        || document.body.scrollWidth > document.body.clientWidth,
      pageState: document.querySelector("#yunos-desktop-shell")?.dataset.pageState,
      sidebarMode: document.querySelector("#yunos-desktop-shell")?.dataset.sidebarMode,
    }));
    assert(measurement.title.trim(), `legacy view title missing: ${view}`);
    assert(measurement.contentWidth <= 1081, `legacy content width drifted: ${view}`);
    assert(!measurement.horizontalOverflow, `legacy view overflow: ${view}`);
    assert(measurement.pageState === "workspace" && measurement.sidebarMode === "push", `legacy push state drifted: ${view}`);
    const path = await screenshot(`legacy-${view}-1600x900`);
    legacyViews.push({ view, ...measurement, screenshot: path });
  }

  let releaseStaleRecent;
  let markStaleRecentIntercepted;
  const staleRecentIntercepted = new Promise((resolve) => { markStaleRecentIntercepted = resolve; });
  const staleRecentRelease = new Promise((resolve) => { releaseStaleRecent = resolve; });
  await page.route("**/api/recent", async (route) => {
    markStaleRecentIntercepted();
    await staleRecentRelease;
    await route.continue();
  }, { times: 1 });
  const staleRecentResponse = page.waitForResponse((response) => response.url().includes("/api/recent"));
  await page.locator('[data-view="recent"]').click();
  await staleRecentIntercepted;
  await page.locator('[data-view="about"]').click();
  await waitForLegacyViewReady("about");
  releaseStaleRecent();
  await staleRecentResponse;
  await waitForAnimationFrames();
  assert(await page.locator('[data-view="about"][aria-current="page"]').count() === 1, "stale recent request changed the active view");
  assert((await page.locator("#view-title").textContent())?.trim() === "关于系统", "stale recent request replaced the current title");
  assert(await page.getByRole("heading", { name: "AyunOS 本地第二大脑原型" }).count() === 1, "stale recent response replaced the about view");
  await page.unroute("**/api/recent");

  await page.locator('[data-view="recent"]').click();
  await waitForLegacyViewReady("recent");
  const firstCard = page.locator("#content .card.clickable").first();
  await firstCard.waitFor({ state: "visible" });
  assert(await firstCard.getAttribute("role") === "link", "legacy card role missing");
  assert(await firstCard.getAttribute("tabindex") === "0", "legacy card keyboard target missing");
  await firstCard.focus();
  await page.keyboard.press("Enter");
  await page.getByRole("heading", { name: "原文对话详情" }).waitFor();

  await page.setViewportSize({ width: 1280, height: 560 });
  if ((await shellState()).sidebarOpen !== "true") await page.locator("#shell-sidebar-toggle").click();
  await waitForSidebarVisualState(true);
  for (const selector of ["#sidebar-settings", "#sidebar-system-tools"]) {
    const details = page.locator(selector);
    if ((await details.getAttribute("open")) === null) await details.locator(":scope > summary").click();
  }
  const sidebarScroll = await page.locator("#shell-sidebar").evaluate((node) => ({
    clientHeight: node.clientHeight,
    scrollHeight: node.scrollHeight,
    overflowY: getComputedStyle(node).overflowY,
    scrollbarWidth: getComputedStyle(node).scrollbarWidth,
  }));
  assert(sidebarScroll.overflowY === "auto", "sidebar internal scrolling is disabled");
  assert(sidebarScroll.scrollHeight > sidebarScroll.clientHeight, "low-height sidebar did not exercise internal scrolling");
  const firstNav = page.locator('[data-view="search"]');
  await firstNav.focus();
  await page.keyboard.press("Tab");
  assert(await page.evaluate(() => document.activeElement?.matches('[data-view="timeline"]')), "sidebar keyboard order drifted");
  const lastNav = page.locator('[data-view="about"]');
  await lastNav.scrollIntoViewIfNeeded();
  const lastVisibility = await page.locator("#shell-sidebar").evaluate((sidebar) => {
    const target = sidebar.querySelector('[data-view="about"]');
    const sidebarRect = sidebar.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    return { top: targetRect.top - sidebarRect.top, bottom: targetRect.bottom - sidebarRect.top, height: sidebar.clientHeight };
  });
  assert(lastVisibility.top >= 0 && lastVisibility.bottom <= lastVisibility.height + 1, "last system tool is clipped");
  await screenshot("sidebar-low-height-scroll-1280x560");

  assert(executeRequests.length === 0, `read-only acceptance emitted execute POSTs: ${executeRequests.length}`);
  assert(externalRequests.length === 0, `unexpected external requests: ${JSON.stringify(externalRequests)}`);
  assert(consoleErrors.length === 0, `console errors: ${JSON.stringify(consoleErrors)}`);
  assert(pageErrors.length === 0, `page errors: ${JSON.stringify(pageErrors)}`);
  assert(failedResponses.length === 0, `failed responses: ${JSON.stringify(failedResponses)}`);

  const report = {
    generatedAt: new Date().toISOString(),
    baseUrl,
    fixedViewports,
    fixedMeasurements,
    continuousRange: { min: 1000, max: 2560, samples: continuousMeasurements.length },
    continuousMeasurements,
    preview: {
      status: preview.status,
      reviewedBindingIdentity: preview.reviewed_execution_binding?.identity,
      artifactIdentity: preview.request_artifact?.artifact_identity,
      deliveryIdentity: preview.delivery_identity,
      assemblyIdentity: preview.delivery?.assembly_binding?.assembly_identity,
      sealedPayloadPubliclyExposed: "payload" in (preview.request_artifact || {}),
      credentialRead: preview.execution?.credential_read,
      providerCalls: preview.execution?.external_model_calls,
      networkRequests: preview.execution?.network_request_count,
    },
    legacyViews,
    sidebarLowHeight: { ...sidebarScroll, lastVisibility },
    executePosts: executeRequests.length,
    externalRequests,
    consoleErrors,
    pageErrors,
    failedResponses,
    screenshots,
  };
  const downloadPromise = page.waitForEvent("download");
  await page.evaluate((content) => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([content], { type: "application/json" }));
    link.download = "browser-acceptance.json";
    link.click();
  }, JSON.stringify(report, null, 2));
  const download = await downloadPromise;
  await download.saveAs(`${artifactDir}/browser-acceptance.json`);
  return report;
}
