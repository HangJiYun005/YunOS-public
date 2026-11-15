async (page) => {
  const baseUrl = "http://127.0.0.1:8765/";
  const artifactDir = "output/playwright/v8.1-a2-responsive-continuity";
  const consoleErrors = [];
  const ignoredConsoleErrors = [];
  const pageErrors = [];
  const failedResponses = [];

  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const entry = { text: message.text(), location: message.location() };
    if (entry.location.url.endsWith("/favicon.ico") && entry.text.includes("404")) {
      ignoredConsoleErrors.push(entry);
      return;
    }
    consoleErrors.push(entry);
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) failedResponses.push({ status: response.status(), url: response.url() });
  });

  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  function expectedScale(width) {
    return {
      omniboxWidth: Math.min(864, Math.max(580, 0.28604924 * width + 131.713951)),
      omniboxHeight: Math.min(67.2, Math.max(52, 0.01547479 * width + 27.584525)),
      logoFontSize: Math.min(28.5, Math.max(19, 0.01113716 * width - 0.011137)),
      navFontSize: 14,
    };
  }

  async function dismissSplash(context) {
    const splash = context.locator("#splash");
    if (!(await splash.count())) return;
    if (await splash.isVisible()) await splash.click();
    await splash.waitFor({ state: "detached", timeout: 5000 });
  }

  async function measure(context) {
    return context.evaluate(() => {
      const shell = document.querySelector("#yunos-desktop-shell");
      const omnibox = document.querySelector(".yunos-shell-omnibox-anchor");
      const logo = document.querySelector(".yunos-shell-logo-word");
      const nav = document.querySelector(".nav-item:not(.active)") || document.querySelector(".nav-item");
      const rootStyle = getComputedStyle(document.documentElement);
      const box = omnibox.getBoundingClientRect();
      const navStyle = getComputedStyle(nav);
      const sidebarStyle = getComputedStyle(document.querySelector("#shell-sidebar"));
      const rgbChannels = (color) => (color.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
      const relativeLuminance = (color) => {
        const linear = rgbChannels(color).map((channel) => {
          const value = channel / 255;
          return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
      };
      const contrastRatio = (first, second) => {
        const [lighter, darker] = [relativeLuminance(first), relativeLuminance(second)].sort((a, b) => b - a);
        return (lighter + 0.05) / (darker + 0.05);
      };
      const tiers = {
        narrow: matchMedia("(width < 1360px)").matches,
        compact: matchMedia("(1360px <= width < 1800px)").matches,
        wide: matchMedia("(width >= 1800px)").matches,
      };
      const matchedTiers = Object.entries(tiers).filter(([, matches]) => matches).map(([name]) => name);
      return {
        layoutViewportWidth: innerWidth,
        visualViewportWidth: window.visualViewport?.width ?? innerWidth,
        documentClientWidth: document.documentElement.clientWidth,
        omnibox: { x: box.x, y: box.y, width: box.width, height: box.height },
        logoFontSize: parseFloat(getComputedStyle(logo).fontSize),
        navFontSize: parseFloat(navStyle.fontSize),
        navColor: navStyle.color,
        navBackgroundColor: sidebarStyle.backgroundColor,
        navContrast: contrastRatio(navStyle.color, sidebarStyle.backgroundColor),
        baseFontSize: parseFloat(getComputedStyle(document.body).fontSize),
        tier: matchedTiers[0] || "none",
        matchedTierCount: matchedTiers.length,
        tierMatches: tiers,
        sidebarWidth: document.querySelector("#shell-sidebar").getBoundingClientRect().width,
        pageState: shell.dataset.pageState,
        sidebarMode: shell.dataset.sidebarMode,
        sidebarOpen: shell.dataset.sidebarOpen,
        scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
        hasHorizontalOverflow:
          Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) >
          document.documentElement.clientWidth,
      };
    });
  }

  function verifyMeasurement(result, expectedTier) {
    const expected = expectedScale(result.visualViewportWidth);
    assert(result.matchedTierCount === 1, `expected exactly one media tier at ${result.visualViewportWidth}px`);
    assert(result.tier === expectedTier, `expected ${expectedTier}, got ${result.tier}`);
    assert(!result.hasHorizontalOverflow, `horizontal overflow at ${result.visualViewportWidth}px`);
    assert(Math.abs(result.omnibox.width - expected.omniboxWidth) <= 0.75, `unexpected omnibox width at ${result.visualViewportWidth}: ${result.omnibox.width} != ${expected.omniboxWidth}`);
    assert(Math.abs(result.omnibox.height - expected.omniboxHeight) <= 0.25, `unexpected omnibox height at ${result.visualViewportWidth}: ${result.omnibox.height} != ${expected.omniboxHeight}`);
    assert(Math.abs(result.logoFontSize - expected.logoFontSize) <= 0.1, "unexpected logo font size");
    assert(Math.abs(result.navFontSize - expected.navFontSize) <= 0.1, "unexpected nav font size");
    assert(result.navContrast >= 4.5, "muted navigation contrast is below 4.5:1");
  }

  await page.setViewportSize({ width: 1359, height: 768 });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await dismissSplash(page);

  const viewportCases = [
    { width: 1359, height: 768, tier: "narrow" },
    { width: 1707, height: 1067, tier: "compact" },
    { width: 1798, height: 1000, tier: "compact" },
    { width: 1799, height: 1000, tier: "compact" },
    { width: 1800, height: 1000, tier: "wide" },
    { width: 1801, height: 1000, tier: "wide" },
    { width: 2560, height: 1440, tier: "wide" },
  ];
  const cases = [];

  for (const viewport of viewportCases) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.waitForTimeout(280);
    const result = await measure(page);
    verifyMeasurement(result, viewport.tier);
    cases.push({ requestedViewport: viewport, ...result });
    await page.screenshot({
      path: `${artifactDir}/home-${viewport.width}x${viewport.height}.png`,
      fullPage: false,
    });
  }

  const boundaryCases = cases.filter(({ requestedViewport }) =>
    [1798, 1799, 1800, 1801].includes(requestedViewport.width)
  );
  for (let index = 1; index < boundaryCases.length; index += 1) {
    const previous = boundaryCases[index - 1];
    const current = boundaryCases[index];
    assert(Math.abs(current.omnibox.width - previous.omnibox.width) < 0.75, "omnibox width jumped at boundary");
    assert(Math.abs(current.omnibox.height - previous.omnibox.height) < 0.1, "omnibox height jumped at boundary");
    assert(Math.abs(current.logoFontSize - previous.logoFontSize) < 0.1, "logo font jumped at boundary");
    assert(Math.abs(current.navFontSize - previous.navFontSize) < 0.1, "nav font jumped at boundary");
  }

  await page.setViewportSize({ width: 1800, height: 1000 });
  await page.waitForTimeout(280);
  const overlayBefore = await measure(page);
  await page.locator("#shell-sidebar-toggle").click();
  await page.waitForTimeout(300);
  const overlayAfter = await measure(page);
  assert(overlayAfter.pageState === "home" && overlayAfter.sidebarMode === "overlay", "overlay state changed");
  for (const key of ["x", "y", "width", "height"]) {
    assert(Math.abs(overlayAfter.omnibox[key] - overlayBefore.omnibox[key]) <= 0.1, `overlay moved omnibox ${key}`);
  }
  await page.screenshot({ path: `${artifactDir}/overlay-1800x1000.png`, fullPage: false });

  const systemTools = page.locator("#sidebar-system-tools");
  if ((await systemTools.getAttribute("open")) === null) {
    await systemTools.locator(":scope > summary").click();
  }
  await page.locator('[data-view="recent"]').click();
  await page.waitForTimeout(400);
  const pushOpen = await page.evaluate(() => {
    const shell = document.querySelector("#yunos-desktop-shell");
    const stage = document.querySelector(".yunos-shell-stage");
    return {
      pageState: shell.dataset.pageState,
      sidebarMode: shell.dataset.sidebarMode,
      sidebarOpen: shell.dataset.sidebarOpen,
      stagePaddingLeft: parseFloat(getComputedStyle(stage).paddingLeft),
      sidebarWidth: document.querySelector("#shell-sidebar").getBoundingClientRect().width,
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
      clientWidth: document.documentElement.clientWidth,
    };
  });
  assert(pushOpen.pageState === "workspace" && pushOpen.sidebarMode === "push", "workspace is not push mode");
  assert(pushOpen.sidebarOpen === "true", "workspace sidebar did not remain open");
  assert(Math.abs(pushOpen.stagePaddingLeft - pushOpen.sidebarWidth) <= 0.1, "push stage padding is incorrect");
  assert(pushOpen.scrollWidth <= pushOpen.clientWidth, "push mode overflowed horizontally");

  await page.locator("#shell-sidebar-toggle").click();
  await page.waitForTimeout(300);
  const pushClosedPadding = await page.locator(".yunos-shell-stage").evaluate((stage) =>
    parseFloat(getComputedStyle(stage).paddingLeft)
  );
  assert(pushClosedPadding <= 0.1, "push stage did not return after close");
  await page.locator("#shell-sidebar-toggle").click();
  await page.waitForTimeout(300);
  const pushReopenedPadding = await page.locator(".yunos-shell-stage").evaluate((stage) =>
    parseFloat(getComputedStyle(stage).paddingLeft)
  );
  assert(Math.abs(pushReopenedPadding - pushOpen.sidebarWidth) <= 0.1, "push stage did not restore after reopen");
  await page.screenshot({ path: `${artifactDir}/push-1800x1000.png`, fullPage: false });

  await page.setViewportSize({ width: 2000, height: 1100 });
  await page.setContent(
    `<iframe id="fractional-frame" src="${baseUrl}" style="display:block;width:1799.49px;height:1000px;border:0"></iframe>`
  );
  const frameElement = await page.locator("#fractional-frame").elementHandle();
  const frame = await frameElement.contentFrame();
  await frame.waitForSelector("#yunos-desktop-shell");
  await dismissSplash(frame);
  await frame.waitForTimeout(280);
  const fractional = await measure(frame);
  fractional.requestedViewportWidth = 1799.49;
  fractional.hostFrameWidth = await page.locator("#fractional-frame").evaluate((element) =>
    element.getBoundingClientRect().width
  );
  verifyMeasurement(fractional, "compact");
  assert(Math.abs(fractional.hostFrameWidth - 1799.49) < 0.02, "fractional iframe width was not preserved");
  await page.screenshot({ path: `${artifactDir}/home-fractional-1799.49.png`, fullPage: false });

  assert(consoleErrors.length === 0, `console errors: ${JSON.stringify(consoleErrors)}`);
  assert(pageErrors.length === 0, `page errors: ${pageErrors.join(" | ")}`);

  const report = {
    generatedAt: new Date().toISOString(),
    cases,
    fractional,
    overlay: { before: overlayBefore, after: overlayAfter },
    push: { open: pushOpen, closedPadding: pushClosedPadding, reopenedPadding: pushReopenedPadding },
    consoleErrors,
    ignoredConsoleErrors,
    pageErrors,
    failedResponses,
  };
  const reportJson = JSON.stringify(report, null, 2);
  const downloadPromise = page.waitForEvent("download");
  await page.evaluate((content) => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([content], { type: "application/json" }));
    link.download = "measurements.json";
    link.click();
  }, reportJson);
  const download = await downloadPromise;
  await download.saveAs(`${artifactDir}/measurements.json`);
  console.log(reportJson);
  return report;
}
