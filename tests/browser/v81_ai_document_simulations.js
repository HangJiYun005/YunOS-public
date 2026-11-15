async (page) => {
  const baseUrl = "http://127.0.0.1:8781/";
  const artifactDir = "E:/AyunOS/output/playwright/v8.1-d-ai-document-result";
  const results = [];

  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  async function dismissSplash(target) {
    const splash = target.locator("#splash");
    if (!(await splash.count())) return;
    if (await splash.isVisible()) await splash.click();
    await splash.waitFor({ state: "detached", timeout: 5000 });
  }

  async function capture(name, status, finishReason = null) {
    const target = await page.context().newPage();
    const consoleErrors = [];
    const pageErrors = [];
    let preview = null;
    let executePosts = 0;
    target.on("console", (message) => {
      if (message.type() !== "error") return;
      const location = message.location();
      if (location.url?.endsWith("/favicon.ico") && message.text().includes("404")) return;
      consoleErrors.push({ text: message.text(), location });
    });
    target.on("pageerror", (error) => pageErrors.push(error.message));
    await target.route("**/api/runtime-execute", async (route) => {
      executePosts += 1;
      const body = route.request().postDataJSON();
      const succeeded = status === "succeeded";
      const result = {
        protocol_version: preview.protocol_version,
        model_run_id: preview.diagnostic_bindings.model_run_id,
        conversation_id: preview.diagnostic_bindings.conversation_id,
        request_id: body.request_id,
        mutation_id: body.mutation_id,
        status,
        execution_phase: succeeded ? "succeeded" : "provider_failed",
        delivery_identity: preview.delivery_identity,
        request_artifact: preview.request_artifact,
        provider: preview.runtime_profile.provider,
        model: preview.runtime_profile.model,
        usage: succeeded ? { prompt_tokens: 96, completion_tokens: 24, total_tokens: 120 } : null,
        response: succeeded ? {
          message_id: `msg_${"3".repeat(64)}`,
          content: "这是用于验证截断提示的模拟正文；它不是正式 Runtime 执行记录。",
          provider_response_id: "simulated-browser-response",
          finish_reason: finishReason,
        } : null,
        error: succeeded ? null : {
          error_id: `err_${"4".repeat(64)}`,
          category: "provider",
          code: "simulated_provider_error",
          retryable: false,
          http_status: 503,
        },
        execution: { persisted: true, sent_to_model: true, external_model_calls: true },
        candidate_hook: { status: "reserved", invoked: false },
        receipt_identity: `rrc_${succeeded ? "5" : "6"}`.padEnd(68, succeeded ? "5" : "6"),
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          protocol_version: preview.protocol_version,
          status,
          execution_phase: result.execution_phase,
          result,
          transport: {
            adapter_constructor_count_this_request: 1,
            adapter_call_count_this_request: 1,
            model_call_count_for_run: 1,
            exactly_one_call: true,
            automatic_retry: false,
            mock_transport: true,
            network_request_count: 0,
            credential_read: false,
          },
        }),
      });
    });
    await target.setViewportSize({ width: 1440, height: 900 });
    await target.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await dismissSplash(target);
    await target.evaluate(() => sessionStorage.removeItem("yunos.homeAiDocument.session.v1"));
    await target.locator('[data-omnibox-mode="ai"]').click();
    await target.locator("#home-omnibox-input").fill("MacBook");
    const previewPromise = target.waitForResponse(
      (response) => response.url().includes("/api/runtime-preview?"),
    );
    await target.locator("#home-omnibox-input").press("Enter");
    const response = await previewPromise;
    assert(response.ok(), `${name} preview failed`);
    preview = await response.json();
    await target.locator('#home-ai-document[data-ai-state="preview-ready"]').waitFor({ state: "visible" });
    await target.locator("#home-ai-execute").click();
    const expectedState = status === "failed" ? "provider-error" : "truncated";
    await target.locator(`#home-ai-document[data-ai-state="${expectedState}"]`).waitFor({ state: "visible" });
    assert(executePosts === 1, `${name} execute POST count is not exactly one`);
    await target.evaluate((label) => {
      const body = document.querySelector(".yunos-ai-document-body");
      const notice = document.createElement("p");
      notice.className = "yunos-ai-state-notice is-warning";
      notice.textContent = `${label}：仅用于 UI 状态验收，不是正式 Runtime 执行记录。`;
      body.prepend(notice);
    }, name);
    const visible = await target.locator("#home-ai-document-content").innerText();
    if (expectedState === "truncated") {
      assert(visible.includes("回答可能被截断"), "truncated warning missing");
      assert(visible.includes("不会自动续写或自动重试"), "truncated retry boundary missing");
    } else {
      assert(visible.includes("simulated_provider_error"), "provider error code hidden");
      assert(!visible.includes("AI DOCUMENT RESULT"), "provider error disguised as success");
    }
    const screenshot = `${artifactDir}/${name}.png`;
    await target.screenshot({ path: screenshot, fullPage: false });
    assert(consoleErrors.length === 0, `${name} console errors: ${JSON.stringify(consoleErrors)}`);
    assert(pageErrors.length === 0, `${name} page errors: ${JSON.stringify(pageErrors)}`);
    results.push({ name, state: expectedState, simulated: true, executePosts, screenshot });
    await target.close();
  }

  await capture("simulated-truncated", "succeeded", "length");
  await capture("simulated-provider-error", "failed");
  await page.evaluate((value) => {
    localStorage.setItem("yunos.v81d.acceptance.simulations", JSON.stringify(value));
  }, results);
  return results;
}
