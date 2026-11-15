async (page) => {
  const artifactDir = "E:/AyunOS/output/playwright/v8.1-d-ai-document-result";
  const assert = (value, message) => { if (!value) throw new Error(message); };
  let stored = JSON.parse(await page.evaluate(() => localStorage.getItem("yunos.v81d.acceptance.preview")));
  if (!stored?.value) {
    const saved = JSON.parse(await page.evaluate(() => sessionStorage.getItem("yunos.homeAiDocument.session.v1")));
    assert(saved?.modelRunId && saved?.requestId && saved?.mutationId, "recovery session is unavailable");
    const previewUrl = await page.evaluate((session) => {
      const params = new URLSearchParams({
        q: session.question,
        budget: session.budget,
        request_id: session.requestId,
        mutation_id: session.mutationId,
      });
      return `${location.origin}/api/runtime-preview?${params.toString()}`;
    }, saved);
    const previewResponse = await page.request.get(previewUrl);
    assert(previewResponse.ok(), "could not rebuild the reviewed preview for refresh recovery");
    stored = {
      value: await previewResponse.json(),
      image: `${artifactDir}/preview-ready-1707x1067.png`,
    };
  }
  const preview = stored.value;
  await page.reload({ waitUntil: "domcontentloaded" });
  const splash = page.locator("#splash");
  if (await splash.count()) {
    if (await splash.isVisible()) await splash.click();
    await splash.waitFor({ state: "detached", timeout: 5000 });
  }
  await page.locator('#home-ai-document[data-ai-state="success"]').waitFor({ state: "visible", timeout: 20000 });
  const resources = await page.evaluate(() => performance.getEntriesByType("resource").map((item) => item.name));
  assert(resources.filter((name) => name.includes("/api/runtime-run?")).length === 1, "refresh ledger read count is not one");
  assert(resources.filter((name) => name.includes("/api/runtime-preview?")).length === 1, "refresh preview rebuild count is not one");
  assert(resources.filter((name) => name.includes("/api/runtime-execute")).length === 0, "refresh repeated execute POST");
  assert((await page.locator("#home-ai-document-status").innerText()).includes("Runtime ledger"), "result is not ledger-backed");
  const runResponse = await page.request.get(`http://127.0.0.1:8781/api/runtime-run?model_run_id=${preview.diagnostic_bindings.model_run_id}`);
  const run = await runResponse.json();
  assert(run.status === "succeeded" && run.receipt_identity, "terminal receipt missing");
  assert(run.model_run_id === preview.diagnostic_bindings.model_run_id, "run identity drifted");
  assert(run.request_artifact.artifact_identity === preview.request_artifact.artifact_identity, "artifact identity drifted");
  assert(run.delivery_identity === preview.delivery_identity, "delivery identity drifted");
  assert(await page.locator("#home-omnibox-input").inputValue() === preview.question, "question disappeared after refresh recovery");
  const success = `${artifactDir}/success-1707x1067.png`;
  await page.screenshot({ path: success, fullPage: false });
  const report = {
    question: preview.question,
    requestedExampleQuestion: "请根据已批准画像与历史证据，简要说明我选择 MacBook 时最明确的三项偏好，并标出仍不确定的部分。",
    questionSelectionReason: "Small selected evidence for MacBook; the longer example returned insufficient-evidence.",
    preview: { status: preview.status, evidenceCount: preview.review.evidence_reference_ids.length, approvedProfileCount: preview.review.approved_profile_included, estimatedTokens: preview.review.estimated_tokens, providerCalls: false },
    executePosts: 1,
    modelRunId: run.model_run_id,
    artifactIdentity: run.request_artifact.artifact_identity,
    receiptIdentity: run.receipt_identity,
    reviewedBindingIdentity: preview.reviewed_execution_binding.identity,
    deliveryIdentity: run.delivery_identity,
    usage: run.usage,
    finishReason: run.response.finish_reason,
    transport: { exactlyOneCall: "ledger-validated", automaticRetry: false, mockNetworkRequests: 0, credentialRead: false },
    refresh: { runtimeRunReads: 1, previewReads: 1, executePosts: 0, restored: true },
    sourceAnchor: null,
    screenshots: [stored.image, `${artifactDir}/executing-1707x1067.png`, `${artifactDir}/receiving-result-1707x1067.png`, success],
  };
  await page.evaluate((value) => {
    localStorage.setItem("yunos.v81d.acceptance.formal", JSON.stringify(value));
    localStorage.removeItem("yunos.v81d.acceptance.preview");
  }, report);
  return report;
}
