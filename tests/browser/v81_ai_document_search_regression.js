async (page) => {
  const target = await page.context().newPage();
  const assert = (value, message) => { if (!value) throw new Error(message); };
  await target.setViewportSize({ width: 1280, height: 720 });
  await target.goto("http://127.0.0.1:8781/", { waitUntil: "domcontentloaded" });
  const splash = target.locator("#splash");
  if (await splash.count()) {
    if (await splash.isVisible()) await splash.click();
    await splash.waitFor({ state: "detached", timeout: 5000 });
  }
  await target.evaluate(() => {
    sessionStorage.clear();
    performance.clearResourceTimings();
  });
  await target.locator('[data-omnibox-mode="search"]').click();
  await target.locator("#home-omnibox-input").fill("YunOS");
  const responsePromise = target.waitForResponse((response) => response.url().includes("/api/search?"));
  await target.locator("#home-omnibox-input").press("Enter");
  const response = await responsePromise;
  await target.locator("#home-memory-search-overlay").waitFor({ state: "visible" });
  const paths = await target.evaluate(() => performance.getEntriesByType("resource").map((item) => item.name));
  assert(response.ok(), "memory search request failed");
  assert(paths.filter((name) => name.includes("/api/search?")).length === 1, "memory search request count is not one");
  assert(paths.filter((name) => name.includes("/api/runtime-preview")).length === 0, "search entered AI preview");
  assert(paths.filter((name) => name.includes("/api/runtime-execute")).length === 0, "search entered AI execution");
  assert(await target.locator(".yunos-home-memory-search-result").count() > 0, "memory search returned no visible results");
  await target.close();
  return { searchRequests: 1, aiPreviewRequests: 0, executeRequests: 0 };
}
