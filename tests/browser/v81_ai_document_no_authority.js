async (page) => {
  const baseUrl = "http://127.0.0.1:8781/";
  const target = await page.context().newPage();
  const consoleErrors = [];
  const pageErrors = [];
  let executePosts = 0;

  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  target.on("console", (message) => {
    if (message.type() !== "error") return;
    const location = message.location();
    if (location.url?.endsWith("/favicon.ico") && message.text().includes("404")) return;
    consoleErrors.push({ text: message.text(), location });
  });
  target.on("pageerror", (error) => pageErrors.push(error.message));
  await target.route("**/api/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        db_mode: "real",
        service_mode: "runtime-review",
        readonly: true,
        readonly_source_tables: true,
        runtime_review: true,
        runtime_admin: false,
        runtime_mock: false,
        execution_authority_present: false,
        can_execute: false,
        runtime_sidecar: { ready: true },
      }),
    });
  });
  await target.route("**/api/runtime-execute", async (route) => {
    executePosts += 1;
    await route.abort();
  });

  await target.setViewportSize({ width: 1440, height: 900 });
  await target.goto(baseUrl, { waitUntil: "domcontentloaded" });
  const splash = target.locator("#splash");
  if (await splash.isVisible()) await splash.click();
  await splash.waitFor({ state: "detached", timeout: 5000 });
  await target.evaluate(() => sessionStorage.removeItem("yunos.homeAiDocument.session.v1"));
  await target.locator('[data-omnibox-mode="ai"]').click();
  await target.locator("#home-omnibox-input").fill("MacBook");
  const previewPromise = target.waitForResponse(
    (response) => response.url().includes("/api/runtime-preview?"),
  );
  await target.locator("#home-omnibox-input").press("Enter");
  assert((await previewPromise).ok(), "review-only preview failed");
  await target.locator('#home-ai-document[data-ai-state="preview-ready"]').waitFor({ state: "visible" });
  assert(await target.locator("#home-ai-execute").isDisabled(), "execute remained enabled without authority");
  assert((await target.locator("#home-ai-document-content").innerText()).includes("review-only"), "review-only boundary is not visible");
  await target.locator("#home-ai-execute").evaluate((button) => button.click());
  await target.waitForTimeout(150);
  assert(executePosts === 0, "no-authority state emitted an execute POST");
  assert(consoleErrors.length === 0, `console errors: ${JSON.stringify(consoleErrors)}`);
  assert(pageErrors.length === 0, `page errors: ${JSON.stringify(pageErrors)}`);
  await target.close();
  return { previewCalls: 1, executePosts, authority: false, credentialSurface: false };
}
