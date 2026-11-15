async (page) => {
  const baseUrl = "http://127.0.0.1:8781/";
  const artifactDir = "E:/AyunOS/output/playwright/v8.1-e-contextual-sidebar";
  const viewports = [
    { width: 2560, height: 1440 },
    { width: 1920, height: 1080 },
    { width: 1707, height: 1067 },
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
  const consoleErrors = [];
  const pageErrors = [];
  const failedResponses = [];
  const screenshots = [];
  const viewportMeasurements = [];
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const waitForSidebar = async (open) => {
    await page.locator(`#yunos-desktop-shell[data-sidebar-open="${open}"]`).waitFor();
    await page.waitForTimeout(240);
  };
  const shellState = () => page.locator("#yunos-desktop-shell").evaluate((shell) => ({
    pageState: shell.dataset.pageState,
    mode: shell.dataset.sidebarMode,
    open: shell.dataset.sidebarOpen,
    pinned: shell.dataset.sidebarPinned,
    trigger: shell.dataset.sidebarTrigger,
  }));
  const measureAnchor = (targetPage = page) => targetPage.locator(".yunos-shell-omnibox-anchor").evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  });
  const screenshot = async (name) => {
    const path = `${artifactDir}/${name}.png`;
    await page.screenshot({ path, fullPage: false });
    screenshots.push(path);
    return path;
  };

  page.on("console", (message) => {
    if (message.type() !== "error") return;
    if (message.location().url?.endsWith("/favicon.ico") && message.text().includes("404")) return;
    consoleErrors.push({ text: message.text(), location: message.location() });
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) failedResponses.push({ status: response.status(), url: response.url() });
  });

  await page.setViewportSize({ width: 1707, height: 1067 });
  if (!page.url().startsWith(baseUrl)) await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  const splash = page.locator("#splash");
  if (await splash.count()) {
    if (await splash.isVisible()) await splash.click();
    await splash.waitFor({ state: "detached", timeout: 5000 });
  }

  await page.locator('#home-ai-document[data-ai-state="success"]').waitFor({ state: "visible", timeout: 20000 });
  await page.locator("#shell-sidebar-toggle").click();
  await waitForSidebar("true");
  let state = await shellState();
  assert(state.pageState === "ai" && state.mode === "push", "AI result did not use push mode");
  assert(state.pinned === "true" && state.trigger === "pinned", "AI sidebar was not pinned");
  const aiLabels = await page.locator("#sidebar-context-items .yunos-sidebar-context-label").allTextContents();
  assert(
    ["当前任务", "来源", "相关记忆", "不确定信息", "执行详情"].every((label) => aiLabels.includes(label)),
    `AI context is incomplete: ${JSON.stringify(aiLabels)}`,
  );
  const sourceCount = await page.locator('[data-context-key="sources"] .yunos-sidebar-context-meta').innerText();
  const sourceSummary = await page.locator("#home-ai-section-sources > summary").innerText();
  assert(sourceSummary.endsWith(`· ${sourceCount}`), "sidebar source count is not Runtime-backed");
  await screenshot("04-ai-result-push-1707x1067");

  await page.locator('[data-context-key="sources"]').click();
  assert(await page.locator("#home-ai-section-sources").getAttribute("open") !== null, "source context did not open disclosure");
  await page.keyboard.press("Escape");
  await waitForSidebar("false");
  assert(await page.locator("#shell-sidebar-toggle").evaluate((node) => node === document.activeElement), "Escape did not return focus");

  await page.locator("#home-ai-return-home").click();
  await page.locator('#yunos-desktop-shell[data-page-state="home"]').waitFor();
  assert((await page.locator("#sidebar-context-group").isHidden()), "idle home exposed a context group");
  await screenshot("01-home-sidebar-hidden-1707x1067");

  await page.mouse.move(2, 320);
  await page.waitForTimeout(120);
  assert((await shellState()).open === "false", "hover opened before the delay");
  await page.waitForTimeout(100);
  await waitForSidebar("true");
  state = await shellState();
  assert(state.trigger === "hover" && state.pinned === "false", "hover state was not temporary");
  await screenshot("02-home-hover-overlay-1707x1067");
  await page.mouse.move(900, 700);
  await page.waitForTimeout(180);
  assert((await shellState()).open === "true", "hover closed before leave delay");
  await page.waitForTimeout(120);
  await waitForSidebar("false");

  await page.locator("#shell-sidebar-toggle").click();
  await waitForSidebar("true");
  state = await shellState();
  assert(state.trigger === "pinned" && state.pinned === "true", "Logo/Menu did not pin sidebar");
  await page.mouse.move(900, 700);
  await page.waitForTimeout(320);
  assert((await shellState()).open === "true", "pinned sidebar closed on pointer leave");
  await screenshot("03-home-click-pinned-1707x1067");
  await page.mouse.click(1650, 900);
  await waitForSidebar("false");

  await page.locator('[data-omnibox-mode="search"]').click();
  await page.locator("#home-omnibox-input").fill("YunOS");
  await page.locator("#home-omnibox-input").press("Enter");
  await page.locator("#home-memory-search-overlay").waitFor({ state: "visible" });
  await page.locator("#home-memory-search-results [role='option']").first().waitFor({ state: "visible" });
  const resultCount = await page.locator("#home-memory-search-results [role='option']").count();
  assert(resultCount > 0, "real memory search returned no results");
  await page.locator("#shell-sidebar-toggle").click();
  await waitForSidebar("true");
  const searchLabels = await page.locator("#sidebar-context-items .yunos-sidebar-context-label").allTextContents();
  assert(searchLabels.join("|") === "当前查询|相关记忆", `unexpected search context: ${searchLabels}`);
  assert(
    Number(await page.locator('[data-context-key="related-memories"] .yunos-sidebar-context-meta').innerText()) === resultCount,
    "search context count is not result-backed",
  );
  await screenshot("05-memory-search-context-1707x1067");

  assert(await page.locator("#sidebar-system-tools").getAttribute("open") === null, "system tools were not folded by default");
  await screenshot("06-system-tools-collapsed-1707x1067");
  await page.locator("#sidebar-system-tools > summary").click();
  assert(await page.locator("#sidebar-system-tools").getAttribute("open") !== null, "system tools did not expand");
  await page.locator("#shell-sidebar").evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await screenshot("07-system-tools-expanded-1707x1067");

  assert(await page.locator("#sidebar-settings").getAttribute("open") === null, "settings were not folded by default");
  await page.locator("#sidebar-settings > summary").click();
  assert(await page.locator("#sidebar-settings").getAttribute("open") !== null, "settings did not expand");

  await page.locator("#home-omnibox-input").press("Escape");
  assert(await page.locator("#home-memory-search-overlay").isHidden(), "first Escape did not close search results");
  assert((await shellState()).open === "true", "search Escape incorrectly closed sidebar");

  await page.keyboard.press("Escape");
  await waitForSidebar("false");
  for (const viewport of viewports) {
    const matrixPage = await page.context().newPage();
    matrixPage.on("console", (message) => {
      if (message.type() !== "error") return;
      if (message.location().url?.endsWith("/favicon.ico") && message.text().includes("404")) return;
      consoleErrors.push({ text: message.text(), location: message.location() });
    });
    matrixPage.on("pageerror", (error) => pageErrors.push(error.message));
    matrixPage.on("response", (response) => {
      if (response.status() >= 400) failedResponses.push({ status: response.status(), url: response.url() });
    });
    await matrixPage.setViewportSize(viewport);
    await matrixPage.goto(baseUrl, { waitUntil: "domcontentloaded" });
    const matrixSplash = matrixPage.locator("#splash");
    if (await matrixSplash.count()) {
      if (await matrixSplash.isVisible()) await matrixSplash.click({ force: true });
      await matrixSplash.waitFor({ state: "detached", timeout: 5000 });
    }
    await matrixPage.waitForTimeout(320);
    const before = await measureAnchor(matrixPage);
    await matrixPage.locator("#shell-sidebar-toggle").click();
    await matrixPage.locator('#yunos-desktop-shell[data-sidebar-open="true"]').waitFor();
    await matrixPage.waitForTimeout(240);
    const after = await measureAnchor(matrixPage);
    for (const key of ["x", "y", "width", "height"]) {
      assert(
        Math.abs(after[key] - before[key]) <= 0.1,
        `overlay moved Omnibox ${key} at ${viewport.width}: ${before[key]} -> ${after[key]}`,
      );
    }
    const overflow = await matrixPage.evaluate(() => ({
      html: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      body: document.body.scrollWidth > document.body.clientWidth,
    }));
    assert(!overflow.html && !overflow.body, `overlay overflow at ${viewport.width}`);
    viewportMeasurements.push({ viewport, overlayBefore: before, overlayAfter: after });
    await matrixPage.close();
  }

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.locator("#shell-sidebar-toggle").click();
  await waitForSidebar("true");
  const reducedTransition = await page.locator("#shell-sidebar").evaluate((node) => getComputedStyle(node).transitionDuration);
  assert(reducedTransition.split(",").every((value) => parseFloat(value) <= 0.00001), `reduced motion transition remained: ${reducedTransition}`);
  await page.locator("#shell-sidebar-toggle").click();
  await waitForSidebar("false");
  await page.emulateMedia({ reducedMotion: "no-preference" });

  await page.setViewportSize({ width: 1100, height: 800 });
  await page.locator("#shell-sidebar-toggle").click();
  await waitForSidebar("true");
  if (await page.locator("#sidebar-settings").getAttribute("open") === null) await page.locator("#sidebar-settings > summary").click();
  if (await page.locator("#sidebar-system-tools").getAttribute("open") === null) await page.locator("#sidebar-system-tools > summary").click();
  for (const view of requiredViews) {
    const item = page.locator(`[data-view="${view}"]`);
    await item.scrollIntoViewIfNeeded();
    await item.click();
    assert(await item.getAttribute("aria-current") === "page", `legacy view did not highlight: ${view}`);
  }
  state = await shellState();
  assert(state.pageState === "workspace" && state.mode === "push" && state.pinned === "true", "workspace push state drifted");
  const workspaceGeometry = await page.evaluate(() => {
    const sidebar = document.querySelector("#shell-sidebar").getBoundingClientRect();
    const workspace = document.querySelector("#shell-workspace-region").getBoundingClientRect();
    return {
      sidebarRight: sidebar.right,
      workspaceLeft: workspace.left,
      workspaceWidth: workspace.width,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    };
  });
  assert(workspaceGeometry.workspaceLeft >= workspaceGeometry.sidebarRight - 0.1, "push content overlaps sidebar");
  assert(workspaceGeometry.workspaceWidth >= 760, `workspace too narrow: ${workspaceGeometry.workspaceWidth}`);
  assert(!workspaceGeometry.overflow, "1100px workspace has horizontal overflow");
  await screenshot("08-workspace-push-1100x800");

  assert(consoleErrors.length === 0, `console errors: ${JSON.stringify(consoleErrors)}`);
  assert(pageErrors.length === 0, `page errors: ${JSON.stringify(pageErrors)}`);
  assert(failedResponses.length === 0, `failed responses: ${JSON.stringify(failedResponses)}`);

  const report = {
    generatedAt: new Date().toISOString(),
    baseUrl,
    runtimeMode: "runtime-admin-mock",
    realModelCalls: 0,
    requiredViews,
    screenshots,
    viewportMeasurements,
    workspaceGeometry,
    consoleErrors,
    pageErrors,
    failedResponses,
  };
  const downloadPromise = page.waitForEvent("download");
  await page.evaluate((content) => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([content], { type: "application/json" }));
    link.download = "measurements.json";
    link.click();
  }, JSON.stringify(report, null, 2));
  const download = await downloadPromise;
  await download.saveAs(`${artifactDir}/measurements.json`);
  return report;
}
