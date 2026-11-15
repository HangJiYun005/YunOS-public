async (page) => {
  const artifactDir = "E:/AyunOS/output/playwright/v8.1-d-ai-document-result";
  const stored = JSON.parse(await page.evaluate(() => localStorage.getItem("yunos.v81d.acceptance.preview")));
  const preview = stored.value;
  let routeCalls = 0;
  let envelope = null;
  let body = null;
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const handler = async (route) => {
    routeCalls += 1;
    body = route.request().postDataJSON();
    const forwarded = await route.fetch();
    envelope = await forwarded.json();
    await page.waitForTimeout(1200);
    await route.fulfill({ response: forwarded });
  };
  await page.route("**/api/runtime-execute", handler);
  await page.evaluate(() => {
    const button = document.querySelector("#home-ai-execute");
    button.click();
    button.click();
  });
  await page.locator('#home-ai-document[data-ai-state="executing"]').waitFor({ state: "visible" });
  assert(await page.locator("#home-omnibox-input").inputValue() === preview.question, "question disappeared while executing");
  const executing = `${artifactDir}/executing-1707x1067.png`;
  await page.screenshot({ path: executing, fullPage: false });
  await page.locator('#home-ai-document[data-ai-state="receiving-result"]').waitFor({ state: "visible" });
  assert(await page.locator("#home-omnibox-input").inputValue() === preview.question, "question disappeared while receiving result");
  const receiving = `${artifactDir}/receiving-result-1707x1067.png`;
  await page.screenshot({ path: receiving, fullPage: false });
  await page.locator('#home-ai-document[data-ai-state="success"]').waitFor({ state: "visible" });
  assert(await page.locator("#home-omnibox-input").inputValue() === preview.question, "question disappeared after success");
  await page.unroute("**/api/runtime-execute", handler);
  const success = `${artifactDir}/success-1707x1067.png`;
  await page.screenshot({ path: success, fullPage: false });
  assert(routeCalls === 1, "double click caused more than one execute POST");
  assert(body.q === preview.question && body.budget === preview.budget_profile, "question/budget drifted");
  assert(body.request_id === preview.request_id && body.mutation_id === preview.mutation_id, "request identity drifted");
  assert(body.confirmed === true, "confirmation missing");
  assert(JSON.stringify(body.expected_bindings) === JSON.stringify(preview.reviewed_execution_binding), "Reviewed Binding drifted");
  assert(envelope.status === "succeeded", "mock Runtime failed");
  assert(envelope.transport.exactly_one_call === true && envelope.transport.automatic_retry === false, "exactly one/retry invariant failed");
  assert(envelope.transport.network_request_count === 0 && envelope.transport.credential_read === false, "mock crossed network/credential boundary");
  const report = {
    question: preview.question,
    requestedExampleQuestion: "请根据已批准画像与历史证据，简要说明我选择 MacBook 时最明确的三项偏好，并标出仍不确定的部分。",
    questionSelectionReason: "Small selected evidence for MacBook; the longer example returned insufficient-evidence.",
    preview: { status: preview.status, evidenceCount: preview.review.evidence_reference_ids.length, approvedProfileCount: preview.review.approved_profile_included, estimatedTokens: preview.review.estimated_tokens, providerCalls: false },
    executePosts: routeCalls,
    modelRunId: envelope.result.model_run_id,
    artifactIdentity: envelope.result.request_artifact.artifact_identity,
    receiptIdentity: envelope.result.receipt_identity,
    reviewedBindingIdentity: preview.reviewed_execution_binding.identity,
    deliveryIdentity: preview.delivery_identity,
    usage: envelope.result.usage,
    finishReason: envelope.result.response.finish_reason,
    transport: envelope.transport,
    sourceAnchor: null,
    screenshots: [stored.image, executing, receiving, success],
  };
  await page.evaluate((value) => {
    localStorage.setItem("yunos.v81d.acceptance.formal", JSON.stringify(value));
    localStorage.removeItem("yunos.v81d.acceptance.preview");
  }, report);
  return report;
}
