async (page) => {
  const baseUrl = "http://127.0.0.1:8765/";
  const artifactDir = "E:/AyunOS/output/playwright/v8.1-b-home-omnibox";
  const storageKey = "yunos.homeOmnibox.mode";
  const submitEventName = "yunos:home-omnibox-submit";
  const selectors = {
    shell: "#yunos-desktop-shell",
    form: "#home-omnibox-form",
    input: "#home-omnibox-input",
    anchor: ".yunos-shell-omnibox-anchor",
    mode: "[data-omnibox-mode]",
    sidebarToggle: "#shell-sidebar-toggle",
  };
  const placeholders = {
    search: "搜索记忆",
    ai: "向 YunOS 提问",
  };
  const sampleQueries = {
    search: "查找上周的项目记录",
    ai: "MacBook",
  };
  const viewportCases = [
    { width: 2560, height: 1440, tier: "wide" },
    { width: 1920, height: 1080, tier: "wide" },
    { width: 1707, height: 1067, tier: "compact" },
    { width: 1440, height: 900, tier: "compact" },
    { width: 1280, height: 720, tier: "narrow" },
    { width: 1100, height: 800, tier: "narrow" },
  ];
  const consoleErrors = [];
  const ignoredConsoleErrors = [];
  const pageErrors = [];
  const requests = [];
  const failedResponses = [];
  const screenshots = [];

  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  function recordBrowserErrors(context, source) {
    context.on("console", (message) => {
      if (message.type() !== "error") return;
      const entry = { source, text: message.text(), location: message.location() };
      if (entry.location.url.endsWith("/favicon.ico") && entry.text.includes("404")) {
        ignoredConsoleErrors.push(entry);
        return;
      }
      consoleErrors.push(entry);
    });
    context.on("pageerror", (error) => pageErrors.push({ source, message: error.message }));
  }

  recordBrowserErrors(page, "primary");
  page.on("request", (request) => {
    requests.push({
      method: request.method(),
      resourceType: request.resourceType(),
      url: request.url(),
    });
  });
  page.on("response", (response) => {
    if (response.status() >= 400) {
      failedResponses.push({ status: response.status(), url: response.url() });
    }
  });

  function expectedScale(width) {
    return {
      width: Math.min(864, Math.max(580, 0.28604924 * width + 131.713951)),
      height: Math.min(67.2, Math.max(52, 0.01547479 * width + 27.584525)),
    };
  }

  async function dismissSplash(context) {
    const splash = context.locator("#splash");
    if (!(await splash.count())) return;
    if (await splash.isVisible()) await splash.click();
    await splash.waitFor({ state: "detached", timeout: 5000 });
  }

  async function ensureHomeClosed() {
    const shell = page.locator(selectors.shell);
    assert((await shell.getAttribute("data-page-state")) === "home", "expected home page state");
    if ((await shell.getAttribute("data-sidebar-open")) === "true") {
      await page.locator(selectors.sidebarToggle).click();
      await page.waitForTimeout(300);
    }
  }

  async function setMode(mode) {
    await page.locator(`${selectors.mode}[data-omnibox-mode="${mode}"]`).click();
    await page.locator(selectors.input).fill("");
    await page.evaluate((inputSelector) => {
      const input = document.querySelector(inputSelector);
      if (input instanceof HTMLElement) input.blur();
    }, selectors.input);
    const viewport = page.viewportSize();
    if (viewport) await page.mouse.move(viewport.width - 2, viewport.height - 2);
    await page.waitForTimeout(200);
  }

  async function measure(mode, state) {
    return page.evaluate(({ selectors: currentSelectors, mode: currentMode, state: currentState }) => {
      const shell = document.querySelector(currentSelectors.shell);
      const anchor = document.querySelector(currentSelectors.anchor);
      const form = document.querySelector(currentSelectors.form);
      const input = document.querySelector(currentSelectors.input);
      const modes = form?.querySelector(".yunos-home-omnibox-modes");
      const buttons = [...document.querySelectorAll(currentSelectors.mode)];
      if (!shell || !anchor || !form || !input || !modes || buttons.length !== 2) {
        throw new Error("Omnibox acceptance selectors are incomplete");
      }

      const rect = (element) => {
        const box = element.getBoundingClientRect();
        return {
          x: box.x,
          y: box.y,
          width: box.width,
          height: box.height,
          top: box.top,
          right: box.right,
          bottom: box.bottom,
          left: box.left,
        };
      };
      const formStyle = getComputedStyle(form);
      const anchorStyle = getComputedStyle(anchor);
      const placeholderStyle = getComputedStyle(input, "::placeholder");
      const tiers = {
        narrow: matchMedia("(width < 1360px)").matches,
        compact: matchMedia("(1360px <= width < 1800px)").matches,
        wide: matchMedia("(width >= 1800px)").matches,
      };
      const matchedTiers = Object.entries(tiers)
        .filter(([, matches]) => matches)
        .map(([name]) => name);
      const rootScrollWidth = document.documentElement.scrollWidth;
      const bodyScrollWidth = document.body.scrollWidth;

      return {
        mode: currentMode,
        state: currentState,
        viewport: {
          width: innerWidth,
          height: innerHeight,
          visualWidth: window.visualViewport?.width ?? innerWidth,
          clientWidth: document.documentElement.clientWidth,
        },
        anchor: rect(anchor),
        form: rect(form),
        input: {
          ...rect(input),
          value: input.value,
          placeholder: input.placeholder,
          placeholderOpacity: parseFloat(placeholderStyle.opacity),
          focused: document.activeElement === input,
        },
        modes: {
          ...rect(modes),
          clientWidth: modes.clientWidth,
          scrollWidth: modes.scrollWidth,
        },
        buttons: buttons.map((button) => ({
          mode: button.dataset.omniboxMode,
          pressed: button.getAttribute("aria-pressed"),
          clientWidth: button.clientWidth,
          scrollWidth: button.scrollWidth,
          rect: rect(button),
        })),
        formClientWidth: form.clientWidth,
        formScrollWidth: form.scrollWidth,
        formBorderRadius: parseFloat(formStyle.borderTopLeftRadius),
        anchorBorderRadius: parseFloat(anchorStyle.borderTopLeftRadius),
        formBorderColor: formStyle.borderColor,
        formBoxShadow: formStyle.boxShadow,
        formHasFocus: form.matches(":focus-within"),
        activeElementInsideForm: form.contains(document.activeElement),
        dataMode: form.dataset.mode,
        dataHasValue: form.dataset.hasValue,
        shellState: { ...shell.dataset },
        tier: matchedTiers[0] || "none",
        matchedTierCount: matchedTiers.length,
        rootScrollWidth,
        bodyScrollWidth,
        bodyClientWidth: document.body.clientWidth,
        hasHorizontalOverflow: rootScrollWidth > document.documentElement.clientWidth,
      };
    }, { selectors, mode, state });
  }

  function verifyMeasurement(result, viewport, mode, state) {
    const expected = expectedScale(result.viewport.visualWidth);
    const epsilon = 1;
    assert(result.viewport.width === viewport.width, `layout width mismatch at ${viewport.width}px`);
    assert(result.viewport.height === viewport.height, `layout height mismatch at ${viewport.width}px`);
    assert(result.matchedTierCount === 1, `expected one responsive tier at ${viewport.width}px`);
    assert(result.tier === viewport.tier, `expected ${viewport.tier}, got ${result.tier}`);
    assert(!result.hasHorizontalOverflow, `horizontal overflow at ${viewport.width}px`);
    assert(Math.abs(result.anchor.width - expected.width) <= 0.75, `unexpected width at ${viewport.width}px`);
    assert(Math.abs(result.anchor.height - expected.height) <= 0.25, `unexpected height at ${viewport.width}px`);
    assert(Math.abs(result.form.x - result.anchor.x) <= 0.25, "form and anchor x differ");
    assert(Math.abs(result.form.y - result.anchor.y) <= 0.25, "form and anchor y differ");
    assert(Math.abs(result.form.width - result.anchor.width) <= 0.25, "form and anchor width differ");
    assert(Math.abs(result.form.height - result.anchor.height) <= 0.25, "form and anchor height differ");
    assert(result.formBorderRadius >= result.form.height / 2 - epsilon, "form is not a full capsule");
    assert(result.anchorBorderRadius >= result.anchor.height / 2 - epsilon, "anchor is not a full capsule");
    assert(result.form.left >= -epsilon, "omnibox extends past the left viewport edge");
    assert(result.form.right <= result.viewport.clientWidth + epsilon, "omnibox extends past the right viewport edge");
    assert(result.form.top >= -epsilon, "omnibox extends past the top viewport edge");
    assert(result.form.bottom <= result.viewport.height + epsilon, "omnibox extends past the bottom viewport edge");
    assert(result.input.left >= result.form.left - epsilon, "input escapes the capsule on the left");
    assert(result.input.right <= result.modes.left + epsilon, "input overlaps the mode switch");
    assert(result.modes.right <= result.form.right + epsilon, "mode switch escapes the capsule on the right");
    assert(result.modes.top >= result.form.top - epsilon, "mode switch escapes the capsule on top");
    assert(result.modes.bottom <= result.form.bottom + epsilon, "mode switch escapes the capsule on bottom");
    assert(result.formScrollWidth <= result.formClientWidth, "omnibox content overflows horizontally");
    assert(result.modes.scrollWidth <= result.modes.clientWidth, "mode switch content overflows horizontally");
    assert(result.buttons.every((button) => button.scrollWidth <= button.clientWidth), "mode label is clipped");
    assert(result.dataMode === mode, `expected ${mode} data mode, got ${result.dataMode}`);
    assert(result.input.placeholder === placeholders[mode], `wrong ${mode} placeholder`);
    assert(result.input.placeholderOpacity >= 0.99, `${mode} placeholder contrast is weakened by opacity`);
    assert(
      result.buttons.find((button) => button.mode === mode)?.pressed === "true",
      `${mode} toggle is not selected`,
    );
    assert(
      result.buttons.find((button) => button.mode !== mode)?.pressed === "false",
      `inactive toggle is marked selected in ${mode} mode`,
    );

    if (state === "default") {
      assert(result.input.value === "", `${mode} default state is not empty`);
      assert(result.dataHasValue === "false", `${mode} default value state is wrong`);
      assert(!result.activeElementInsideForm, `${mode} default state still has focus`);
    } else if (state === "focus") {
      assert(result.input.value === "", `${mode} focus state is not empty`);
      assert(result.input.focused && result.formHasFocus, `${mode} focus state is not visible`);
      assert(result.dataHasValue === "false", `${mode} focus value state is wrong`);
    } else {
      assert(result.input.value === sampleQueries[mode], `${mode} input value was not preserved`);
      assert(result.input.focused && result.formHasFocus, `${mode} input state lost focus`);
      assert(result.dataHasValue === "true", `${mode} has-value state is missing`);
    }
  }

  await page.setViewportSize({ width: 1100, height: 800 });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await dismissSplash(page);
  await page.evaluate((key) => window.localStorage.removeItem(key), storageKey);
  await page.reload({ waitUntil: "domcontentloaded" });
  await dismissSplash(page);
  await ensureHomeClosed();
  assert(
    (await page.locator(selectors.form).getAttribute("data-mode")) === "search",
    "empty storage did not use the search default",
  );
  assert(
    (await page.locator(selectors.input).getAttribute("placeholder")) === placeholders.search,
    "empty storage used the wrong default placeholder",
  );

  const measurements = [];
  const overlays = [];

  for (const viewport of viewportCases) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.waitForTimeout(280);
    await ensureHomeClosed();

    for (const mode of ["search", "ai"]) {
      await setMode(mode);
      const defaultStyle = await page.locator(selectors.form).evaluate((form) => {
        const style = getComputedStyle(form);
        return { borderColor: style.borderColor, boxShadow: style.boxShadow };
      });

      const defaultResult = await measure(mode, "default");
      verifyMeasurement(defaultResult, viewport, mode, "default");
      measurements.push(defaultResult);
      const defaultPath = `${artifactDir}/home-${viewport.width}x${viewport.height}-${mode}-default.png`;
      await page.screenshot({ path: defaultPath, fullPage: false });
      screenshots.push(defaultPath);

      await page.locator(selectors.input).focus();
      await page.waitForTimeout(200);
      const focusResult = await measure(mode, "focus");
      verifyMeasurement(focusResult, viewport, mode, "focus");
      assert(
        focusResult.formBorderColor !== defaultStyle.borderColor
          || focusResult.formBoxShadow !== defaultStyle.boxShadow,
        `${mode} focus treatment is not visibly distinct at ${viewport.width}px`,
      );
      measurements.push(focusResult);
      const focusPath = `${artifactDir}/home-${viewport.width}x${viewport.height}-${mode}-focus.png`;
      await page.screenshot({ path: focusPath, fullPage: false });
      screenshots.push(focusPath);

      await page.locator(selectors.input).fill(sampleQueries[mode]);
      await page.waitForTimeout(200);
      const inputResult = await measure(mode, "input");
      verifyMeasurement(inputResult, viewport, mode, "input");
      measurements.push(inputResult);
      const inputPath = `${artifactDir}/home-${viewport.width}x${viewport.height}-${mode}-input.png`;
      await page.screenshot({ path: inputPath, fullPage: false });
      screenshots.push(inputPath);
    }

    await setMode("search");
    const overlayBefore = await measure("search", "default");
    await page.locator(selectors.sidebarToggle).click();
    await page.waitForTimeout(300);
    const overlayAfter = await measure("search", "default");
    assert(
      overlayAfter.shellState.pageState === "home"
        && overlayAfter.shellState.sidebarMode === "overlay"
        && overlayAfter.shellState.sidebarOpen === "true",
      `home overlay state is incorrect at ${viewport.width}px`,
    );
    for (const key of ["x", "y", "width", "height"]) {
      assert(
        Math.abs(overlayAfter.anchor[key] - overlayBefore.anchor[key]) <= 0.1,
        `overlay moved omnibox ${key} at ${viewport.width}px`,
      );
    }
    assert(!overlayAfter.hasHorizontalOverflow, `overlay caused overflow at ${viewport.width}px`);
    overlays.push({ viewport, before: overlayBefore.anchor, after: overlayAfter.anchor });
    await page.locator(selectors.sidebarToggle).click();
    await page.waitForTimeout(300);
  }

  assert(screenshots.length === 36, `expected 36 screenshots, got ${screenshots.length}`);
  assert(measurements.length === 36, `expected 36 measurements, got ${measurements.length}`);

  await page.setViewportSize({ width: 1707, height: 1067 });
  await ensureHomeClosed();
  await setMode("search");
  const input = page.locator(selectors.input);
  const searchMode = page.locator(`${selectors.mode}[data-omnibox-mode="search"]`);
  const aiMode = page.locator(`${selectors.mode}[data-omnibox-mode="ai"]`);

  await input.focus();
  await page.keyboard.press("Tab");
  assert(await searchMode.evaluate((button) => document.activeElement === button), "Tab did not move from input to search");
  await page.keyboard.press("Tab");
  assert(await aiMode.evaluate((button) => document.activeElement === button), "Tab did not move from search to AI");
  await page.keyboard.press("Enter");
  assert((await aiMode.getAttribute("aria-pressed")) === "true", "Enter did not activate the focused AI mode");

  await input.fill("Escape 应保留这段输入");
  await input.focus();
  await page.keyboard.press("Escape");
  assert((await input.inputValue()) === "Escape 应保留这段输入", "Escape changed the input value");
  assert(!(await input.evaluate((element) => document.activeElement === element)), "Escape did not blur the input");

  await page.evaluate((eventName) => {
    window.__yunosHomeOmniboxSubmitEvents = [];
    document.addEventListener(eventName, (event) => {
      window.__yunosHomeOmniboxSubmitEvents.push({
        type: event.type,
        bubbles: event.bubbles,
        cancelable: event.cancelable,
        detail: event.detail,
      });
    });
  }, submitEventName);

  const submitChecks = [];
  for (const mode of ["search", "ai"]) {
    await page.locator(`${selectors.mode}[data-omnibox-mode="${mode}"]`).click();
    await input.fill(sampleQueries[mode]);
    await input.focus();
    const requestCountBefore = requests.length;
    const eventCountBefore = await page.evaluate(() => window.__yunosHomeOmniboxSubmitEvents.length);
    const expectedApiPath = mode === "search" ? "/api/search" : "/api/runtime-preview";
    const responsePromise = page.waitForResponse(
      (response) => response.url().includes(expectedApiPath),
    );
    await page.keyboard.press("Enter");
    const response = await responsePromise;
    assert(response.ok(), `${mode} API failed with ${response.status()}`);
    await page.waitForTimeout(100);
    const requestCountAfter = requests.length;
    const addedRequests = requests.slice(requestCountBefore);
    const events = await page.evaluate(() => window.__yunosHomeOmniboxSubmitEvents);
    if (mode === "search") {
      assert(addedRequests.length === 1, "search Enter must cause exactly one request");
      const requestUrl = addedRequests[0].url;
      assert(requestUrl.startsWith(baseUrl), "search request left the local YunOS origin");
      assert(requestUrl.split("?")[0].endsWith("/api/search"), "search Enter used the wrong API");
    } else {
      assert(addedRequests.length === 1, "AI Enter must cause exactly one review-only preview request");
      const requestUrl = addedRequests[0].url;
      assert(requestUrl.startsWith(baseUrl), "AI preview left the local YunOS origin");
      assert(requestUrl.split("?")[0].endsWith("/api/runtime-preview"), "AI Enter used the wrong preview API");
      const executeRequests = addedRequests.filter(
        (request) => request.url.split("?")[0].endsWith("/api/runtime-execute"),
      );
      assert(executeRequests.length === 0, "AI preview called the execute API before confirmation");
      assert(
        (await page.locator(selectors.shell).getAttribute("data-page-state")) === "ai",
        "AI preview did not enter the document page",
      );
    }
    assert(events.length === eventCountBefore + 1, `${mode} Enter did not emit exactly one CustomEvent`);
    const emitted = events.at(-1);
    assert(emitted.type === submitEventName, `${mode} emitted the wrong event type`);
    assert(emitted.bubbles === true && emitted.cancelable === true, `${mode} event flags are wrong`);
    assert(emitted.detail?.mode === mode, `${mode} event detail has the wrong mode`);
    assert(emitted.detail?.query === sampleQueries[mode], `${mode} event detail has the wrong query`);
    submitChecks.push({
      mode,
      requestCountBefore,
      requestCountAfter,
      addedRequests,
      event: emitted,
    });
  }

  await page.locator("#home-ai-cancel").click();
  await page.locator("#home-ai-return-home").click();
  assert(
    (await page.locator(selectors.shell).getAttribute("data-page-state")) === "home",
    "cancelled AI preview did not return to the home page",
  );
  await aiMode.click();
  assert(
    (await page.evaluate((key) => window.localStorage.getItem(key), storageKey)) === "ai",
    "AI mode was not written to namespaced localStorage",
  );
  await page.reload({ waitUntil: "domcontentloaded" });
  await dismissSplash(page);
  await ensureHomeClosed();
  assert((await page.locator(selectors.form).getAttribute("data-mode")) === "ai", "AI mode did not survive refresh");
  assert((await page.locator(selectors.input).getAttribute("placeholder")) === placeholders.ai, "AI placeholder did not survive refresh");

  await page.evaluate((key) => window.localStorage.setItem(key, "invalid-mode"), storageKey);
  await page.reload({ waitUntil: "domcontentloaded" });
  await dismissSplash(page);
  await ensureHomeClosed();
  assert((await page.locator(selectors.form).getAttribute("data-mode")) === "search", "invalid storage did not fall back to search");
  assert((await page.locator(selectors.input).getAttribute("placeholder")) === placeholders.search, "fallback placeholder is wrong");

  const blockedStoragePage = await page.context().newPage();
  recordBrowserErrors(blockedStoragePage, "storage-unavailable");
  await blockedStoragePage.addInitScript(() => {
    const unavailable = () => {
      throw new DOMException("Storage is unavailable for acceptance testing", "SecurityError");
    };
    Object.defineProperty(Storage.prototype, "getItem", {
      configurable: true,
      value: unavailable,
    });
    Object.defineProperty(Storage.prototype, "setItem", {
      configurable: true,
      value: unavailable,
    });
  });
  await blockedStoragePage.setViewportSize({ width: 1100, height: 800 });
  await blockedStoragePage.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await dismissSplash(blockedStoragePage);
  assert(
    (await blockedStoragePage.locator(selectors.form).getAttribute("data-mode")) === "search",
    "unavailable storage did not fall back to search",
  );
  await blockedStoragePage.locator(`${selectors.mode}[data-omnibox-mode="ai"]`).click();
  assert(
    (await blockedStoragePage.locator(selectors.form).getAttribute("data-mode")) === "ai",
    "mode switching stopped working when storage was unavailable",
  );
  await blockedStoragePage.close();

  assert(consoleErrors.length === 0, `console errors: ${JSON.stringify(consoleErrors)}`);
  assert(pageErrors.length === 0, `page errors: ${JSON.stringify(pageErrors)}`);

  const report = {
    generatedAt: new Date().toISOString(),
    baseUrl,
    artifactDir,
    storageKey,
    submitEventName,
    screenshots,
    measurements,
    overlays,
    keyboard: {
      tabOrder: [selectors.input, `${selectors.mode}[data-omnibox-mode="search"]`, `${selectors.mode}[data-omnibox-mode="ai"]`],
      escape: { blurred: true, valuePreserved: true },
    },
    submitChecks,
    storage: {
      refreshRestored: "ai",
      invalidFallback: "search",
      unavailableFallback: "search",
      unavailableModeSwitch: "ai",
    },
    requestCount: requests.length,
    requests,
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
