async (page) => {
  const baseUrl = "http://127.0.0.1:8781/";
  const artifactDir = "E:/AyunOS/output/playwright/v8.1-d-ai-document-result";
  const question = "MacBook";
  const assert = (value, message) => { if (!value) throw new Error(message); };
  await page.setViewportSize({ width: 1707, height: 1067 });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  const splash = page.locator("#splash");
  if (await splash.count()) {
    if (await splash.isVisible()) await splash.click();
    await splash.waitFor({ state: "detached", timeout: 5000 });
  }
  await page.evaluate(() => {
    sessionStorage.removeItem("yunos.homeAiDocument.session.v1");
    localStorage.setItem("yunos.homeOmnibox.mode", "ai");
    localStorage.removeItem("yunos.v81d.acceptance.preview");
    localStorage.removeItem("yunos.v81d.acceptance.formal");
    localStorage.removeItem("yunos.v81d.acceptance.simulations");
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  if (await splash.count()) {
    if (await splash.isVisible()) await splash.click();
    await splash.waitFor({ state: "detached", timeout: 5000 });
  }
  await page.evaluate(() => performance.clearResourceTimings());
  await page.locator('[data-omnibox-mode="ai"]').click();
  await page.locator("#home-omnibox-input").fill(question);
  const responsePromise = page.waitForResponse((response) => response.url().includes("/api/runtime-preview?"));
  await page.locator("#home-omnibox-input").press("Enter");
  const response = await responsePromise;
  const preview = await response.json();
  await page.locator('#home-ai-document[data-ai-state="preview-ready"]').waitFor({ state: "visible" });
  const flow = await page.evaluate(() => performance.getEntriesByType("resource").map((item) => new URL(item.name).pathname));
  assert(response.ok() && preview.status === "preview", "review-only preview failed");
  assert(preview.execution.external_model_calls === false, "preview called provider");
  assert(preview.execution.credential_read === false && preview.execution.network_request_count === 0, "preview crossed credential/network boundary");
  assert(preview.review.evidence_reference_ids.length > 0, "preview has no evidence");
  assert(flow.filter((item) => item === "/api/runtime-preview").length === 1, "preview request count is not one");
  assert(flow.filter((item) => item === "/api/runtime-execute").length === 0, "preview crossed execute boundary");
  assert(!(await page.locator("#home-ai-execute").isDisabled()), "execute unavailable");
  const screenshot = `${artifactDir}/preview-ready-1707x1067.png`;
  await page.screenshot({ path: screenshot, fullPage: false });
  await page.evaluate(({ preview: value, screenshot: image }) => {
    localStorage.setItem("yunos.v81d.acceptance.preview", JSON.stringify({ value, image }));
  }, { preview, screenshot });
  return { status: preview.status, evidence: preview.review.evidence_reference_ids.length, providerCalls: false };
}
