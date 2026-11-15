async (page) => {
  const artifactDir = "E:/AyunOS/output/playwright/v8.1-d-ai-document-result";
  const viewports = [
    { width: 2560, height: 1440, tier: "wide" },
    { width: 1920, height: 1080, tier: "wide" },
    { width: 1707, height: 1067, tier: "compact" },
    { width: 1440, height: 900, tier: "compact" },
    { width: 1280, height: 720, tier: "compact" },
    { width: 1100, height: 800, tier: "compact" },
  ];
  const consoleErrors = [];
  const pageErrors = [];
  const measurements = [];
  const screenshots = [];

  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  const consoleHandler = (message) => {
    if (message.type() !== "error") return;
    const location = message.location();
    if (location.url?.endsWith("/favicon.ico") && message.text().includes("404")) return;
    consoleErrors.push({ text: message.text(), location });
  };
  const errorHandler = (error) => pageErrors.push(error.message);
  page.on("console", consoleHandler);
  page.on("pageerror", errorHandler);

  const stored = await page.evaluate(() => ({
    formal: localStorage.getItem("yunos.v81d.acceptance.formal"),
    simulations: localStorage.getItem("yunos.v81d.acceptance.simulations"),
  }));
  assert(stored.formal && stored.simulations, "formal or simulated acceptance evidence missing");
  const formal = JSON.parse(stored.formal);
  const simulations = JSON.parse(stored.simulations);

  await page.reload({ waitUntil: "domcontentloaded" });
  const splash = page.locator("#splash");
  if (await splash.count()) {
    if (await splash.isVisible()) await splash.click();
    await splash.waitFor({ state: "detached", timeout: 5000 });
  }
  await page.locator('#home-ai-document[data-ai-state="success"]').waitFor({
    state: "visible",
    timeout: 20000,
  });
  const resources = await page.evaluate(() => performance.getEntriesByType("resource").map((item) => item.name));
  assert(resources.filter((name) => name.includes("/api/runtime-execute")).length === 0, "refresh repeated execute POST");
  assert(resources.filter((name) => name.includes("/api/runtime-run?")).length === 1, "refresh did not read ledger exactly once");
  assert(resources.filter((name) => name.includes("/api/runtime-preview?")).length === 1, "refresh did not rebuild preview exactly once");
  assert((await page.locator("#home-ai-document-status").innerText()).includes("Runtime ledger"), "refresh is not marked ledger-backed");

  for (const viewport of viewports) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.waitForTimeout(220);
    const measured = await page.evaluate(() => {
      const shell = document.querySelector("#yunos-desktop-shell");
      const region = document.querySelector("#shell-home-region");
      const anchor = document.querySelector(".yunos-shell-omnibox-anchor");
      const documentNode = document.querySelector("#home-ai-document-content");
      const answer = document.querySelector(".yunos-ai-answer");
      const anchorRect = anchor.getBoundingClientRect();
      const documentRect = documentNode.getBoundingClientRect();
      const answerRect = answer.getBoundingClientRect();
      return {
        viewport: { width: window.innerWidth, height: window.innerHeight },
        pageState: shell.dataset.pageState,
        sidebarMode: shell.dataset.sidebarMode,
        sidebarOpen: shell.dataset.sidebarOpen,
        anchor: { top: anchorRect.top, right: anchorRect.right, bottom: anchorRect.bottom, left: anchorRect.left },
        document: { width: documentRect.width, left: documentRect.left, right: documentRect.right },
        answerWidth: answerRect.width,
        region: {
          clientHeight: region.clientHeight,
          scrollHeight: region.scrollHeight,
          overflowY: getComputedStyle(region).overflowY,
        },
        horizontalOverflow:
          document.documentElement.scrollWidth > document.documentElement.clientWidth
          || document.body.scrollWidth > document.body.clientWidth,
      };
    });
    assert(measured.viewport.width === viewport.width && measured.viewport.height === viewport.height, `viewport mismatch at ${viewport.width}`);
    assert(measured.pageState === "ai", `AI state lost at ${viewport.width}`);
    assert(measured.sidebarMode === "push", `sidebar is not push at ${viewport.width}`);
    assert(measured.anchor.top >= 0 && measured.anchor.left >= 0, `Omnibox crosses top/left at ${viewport.width}`);
    assert(measured.anchor.right <= viewport.width + 1 && measured.anchor.bottom < viewport.height, `Omnibox crosses right/bottom at ${viewport.width}`);
    assert(measured.document.width <= 821 && measured.answerWidth <= 761, `reading width too large at ${viewport.width}`);
    assert(measured.document.left >= 0 && measured.document.right <= viewport.width + 1, `document clipped at ${viewport.width}`);
    assert(!measured.horizontalOverflow, `horizontal scrollbar at ${viewport.width}`);
    assert(measured.region.overflowY === "auto", `AI page is not scrollable at ${viewport.width}`);
    assert(measured.region.scrollHeight >= measured.region.clientHeight, `AI region geometry invalid at ${viewport.width}`);
    measurements.push({ ...viewport, ...measured });
    const screenshot = `${artifactDir}/final-${viewport.tier}-${viewport.width}x${viewport.height}.png`;
    await page.screenshot({ path: screenshot, fullPage: false });
    screenshots.push(screenshot);
  }
  assert(
    measurements.filter((item) => item.height <= 800).some(
      (item) => item.region.scrollHeight > item.region.clientHeight,
    ),
    "no low-height viewport exercised vertical scrolling",
  );

  await page.setViewportSize({ width: 1100, height: 800 });
  await page.locator("#shell-sidebar-toggle").click();
  await page.waitForTimeout(220);
  assert((await page.locator("#yunos-desktop-shell").getAttribute("data-sidebar-open")) === "true", "push sidebar did not open");
  assert(await page.evaluate(() => document.documentElement.scrollWidth === document.documentElement.clientWidth), "push sidebar added horizontal scrollbar");
  await page.locator("#shell-sidebar-toggle").click();

  assert(consoleErrors.length === 0, `console errors: ${JSON.stringify(consoleErrors)}`);
  assert(pageErrors.length === 0, `page errors: ${JSON.stringify(pageErrors)}`);
  const report = {
    generatedAt: new Date().toISOString(),
    baseUrl: "http://127.0.0.1:8781/",
    formalRuntimeMode: "runtime-admin-mock",
    realDeepSeekCallPerformed: false,
    realDeepSeekCallSkipReason: "v8.0 Runtime fixes max_tokens=1024; v8.1-D permits at most 768",
    formal,
    simulations,
    refresh: {
      executePosts: resources.filter((name) => name.includes("/api/runtime-execute")).length,
      runtimeRunReads: resources.filter((name) => name.includes("/api/runtime-run?")).length,
      previewReads: resources.filter((name) => name.includes("/api/runtime-preview?")).length,
      restored: true,
    },
    measurements,
    screenshots: [...formal.screenshots, ...simulations.map((item) => item.screenshot), ...screenshots],
    consoleErrors,
    pageErrors,
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
  page.off("console", consoleHandler);
  page.off("pageerror", errorHandler);
  return report;
}
