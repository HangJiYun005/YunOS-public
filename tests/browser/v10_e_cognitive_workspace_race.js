async (page, harness = {}) => {
  const baseUrl = harness.baseUrl || "http://127.0.0.1:8784/";
  const cognitiveFixture = harness.cognitiveFixture;
  const failureSentinel = "RUNTIME_RACE_SENTINEL";
  const responseSequence = [];
  const consoleErrors = [];
  const pageErrors = [];
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  assert(cognitiveFixture, "a real disposable Cognitive Run fixture is required");
  assert(cognitiveFixture.agency_level === "act", "fixture must be an Act run");
  assert(cognitiveFixture.title, "fixture title is required for exact binding");

  const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
  const bounded = (promise, label) => Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(
      () => reject(new Error(`${label}_TIMEOUT`)),
      5000,
    )),
  ]);
  let resolveRunsFulfilled;
  const runsFulfilled = new Promise((resolve) => { resolveRunsFulfilled = resolve; });
  let resolveRuntimeFailed;
  const runtimeFailed = new Promise((resolve) => { resolveRuntimeFailed = resolve; });

  page.setDefaultTimeout(20_000);
  page.on("console", (message) => {
    const value = message.text();
    const expected503 = message.type() === "error"
      && value.includes("Failed to load resource")
      && value.includes("503");
    if (message.type() === "error" && !expected503 && !value.includes("favicon.ico")) {
      consoleErrors.push(value);
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.pathname === "/api/cognitive/runs" && response.status() === 200) {
      responseSequence.push("runs:200");
    } else if (url.pathname === "/api/runtime-preview") {
      responseSequence.push(`runtime-preview:${response.status()}`);
    } else if (url.pathname === "/api/cognitive/workspace") {
      responseSequence.push(`workspace:${response.status()}`);
    }
  });

  await page.route("**/api/cognitive/runs?**", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response });
    resolveRunsFulfilled();
  });
  await page.route("**/api/runtime-preview?**", async (route) => {
    await bounded(runsFulfilled, "RUNS_FULFILLED");
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store" },
      body: JSON.stringify({ error: failureSentinel }),
    });
    resolveRuntimeFailed();
  });
  await page.route("**/api/cognitive/workspace?**", async (route) => {
    await bounded(runtimeFailed, "RUNTIME_FAILED");
    await delay(200);
    await route.continue();
  });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  const splash = page.locator("#splash");
  if (await splash.count() && await splash.isVisible()) {
    await splash.click();
    await splash.waitFor({ state: "detached", timeout: 5000 });
  }
  await page.locator('[data-omnibox-mode="ai"]').click();
  await page.locator("#home-omnibox-input").fill(cognitiveFixture.title);
  await page.locator("#home-omnibox-input").press("Enter");

  await page.locator(
    '#home-ai-document[data-ai-state="cancelled-or-unavailable"]',
  ).waitFor({ state: "visible" });
  await page.waitForFunction((sentinel) => (
    document.querySelector('[data-cognitive-section="execution"] code')
      ?.textContent.includes(sentinel)
  ), failureSentinel);
  const beforeWorkspace = await page.evaluate((sentinel) => ({
    phase: document.querySelector("#home-ai-document")?.dataset.aiState,
    title: document.querySelector(".yunos-ai-document-title")?.textContent || "",
    hasFailureTruth: document.querySelector('[data-cognitive-section="execution"] code')
      ?.textContent.includes(sentinel) === true,
    supervisionCount: document.querySelectorAll("[data-cognitive-supervision]").length,
  }), failureSentinel);
  assert(beforeWorkspace.phase === "cancelled-or-unavailable", "Runtime failure was not rendered first");
  assert(beforeWorkspace.hasFailureTruth, "Runtime failure technical truth is missing before hydration");
  assert(beforeWorkspace.supervisionCount === 0, "unbound fallback fabricated Act supervision");

  await page.waitForFunction(({ title, sentinel }) => (
    homeAiState.cognitiveBundleStatus === "ready"
    && cognitiveWorkspaceModel().agencyLevel === "act"
    && document.querySelector(".yunos-ai-document-title")?.textContent.trim() === title
    && document.querySelectorAll("[data-cognitive-section]").length === 10
    && document.querySelectorAll("[data-cognitive-supervision]").length === 1
    && document.querySelector('[data-cognitive-section="execution"] code')
      ?.textContent.includes(sentinel)
  ), { title: cognitiveFixture.title, sentinel: failureSentinel });

  const finalState = await page.evaluate((sentinel) => {
    const summary = Object.fromEntries(Array.from(
      document.querySelectorAll(".yunos-ai-summary-item"),
    ).map((row) => [
      row.querySelector(".yunos-ai-summary-label")?.textContent.trim() || "",
      row.querySelector(".yunos-ai-summary-value")?.textContent.trim() || "",
    ]));
    return {
      phase: document.querySelector("#home-ai-document")?.dataset.aiState,
      title: document.querySelector(".yunos-ai-document-title")?.textContent.trim() || "",
      bundleStatus: homeAiState.cognitiveBundleStatus,
      agencyLevel: cognitiveWorkspaceModel().agencyLevel,
      summary,
      sectionKeys: Array.from(document.querySelectorAll("[data-cognitive-section]"))
        .map((element) => element.dataset.cognitiveSection),
      supervisionCount: document.querySelectorAll("[data-cognitive-supervision]").length,
      failureTechnicalTruth: document.querySelector('[data-cognitive-section="execution"] code')
        ?.textContent.includes(sentinel) === true,
      failureOutcomeTruth: document.querySelector('[data-outcome-field="agent"]')
        ?.textContent.includes("无法连接本机 Runtime 预览服务") === true,
      sourceNote: document.querySelector(".yunos-cognitive-source-note")?.textContent.trim() || "",
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    };
  }, failureSentinel);

  assert(finalState.phase === "cancelled-or-unavailable", "workspace hydration erased Runtime failure phase");
  assert(finalState.title === cognitiveFixture.title, "late workspace did not bind the exact fixture title");
  assert(finalState.bundleStatus === "ready", "authoritative workspace did not become ready");
  assert(finalState.agencyLevel === "act", "late workspace did not restore authoritative Act level");
  assert(finalState.sectionKeys.join(",") === [
    "objective", "success", "prohibited", "context", "plan",
    "decisions", "execution", "verification", "outcome", "learning",
  ].join(","), `10-section contract drifted: ${finalState.sectionKeys.join(",")}`);
  assert(finalState.supervisionCount === 1, "late Act workspace did not restore supervision");
  assert(finalState.failureTechnicalTruth, "late workspace erased Runtime failure technical truth");
  assert(finalState.failureOutcomeTruth, "late workspace erased Runtime failure outcome truth");
  assert(finalState.scrollWidth === finalState.clientWidth, "race result has horizontal overflow");
  assert(responseSequence.join(",") === "runs:200,runtime-preview:503,workspace:200", (
    `response ordering mismatch: ${responseSequence.join(",")}`
  ));
  assert(consoleErrors.length === 0, `console errors: ${consoleErrors.join(" | ")}`);
  assert(pageErrors.length === 0, `page errors: ${pageErrors.join(" | ")}`);

  await page.screenshot({
    path: `${harness.artifactDir}/runtime-failure-late-workspace-act.png`,
    fullPage: true,
  });
  return {
    fixtureTitle: cognitiveFixture.title,
    responseSequence,
    beforeWorkspace,
    finalState,
    consoleErrors,
    pageErrors,
  };
}
