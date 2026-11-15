async (page) => {
  const fallbackBaseUrl = "http://127.0.0.1:8792/";
  const artifactDir = "E:/AyunOS/output/playwright/v8.1-f-final-acceptance";
  const question = "MacBook：三点偏好，一项不确定信息";
  const sessionKey = "yunos.homeAiDocument.session.v1";
  const oneShotSentryKey = "yunos.v81f.realDeepSeek.oneShotSentry.v1";
  const recoveryReportKey = "yunos.v81f.realDeepSeek.recoveryReport.v1";
  const sentryArtifact = `${artifactDir}/real-deepseek-one-shot-sentry.json`;
  const reportArtifact = `${artifactDir}/real-deepseek-acceptance.json`;
  const expectedRuntimeProfile = {
    version: "v8.0-d1-deepseek-production-profile-1",
    provider: "deepseek",
    model: "deepseek-v4-flash",
    base_url: "https://api.deepseek.com",
    stream: false,
    thinking: { type: "disabled" },
  };
  const expectedGenerationConfig = { temperature: 0.0, max_tokens: 1024 };
  const expectedSuccessEvents = [
    "state_prepared",
    "state_running",
    "send_attempt_started",
    "provider_response_received",
    "state_succeeded",
  ];
  const expectedSuccessLedgerDelta = {
    conversations: 1,
    request_artifacts: 1,
    model_runs: 1,
    messages: 2,
    events: 5,
    errors: 0,
    receipts: 1,
  };
  const consoleErrors = [];
  const pageErrors = [];
  const failedResponses = [];
  const browserExternalRequests = [];
  const executeRequests = [];
  const screenshots = [];
  const violations = [];
  const salvageErrors = [];
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const check = (condition, message) => { if (!condition) violations.push(message); };

  page.setDefaultTimeout(30000);
  const currentUrl = page.url();
  const baseUrl = currentUrl.startsWith("http://127.0.0.1:")
    ? `${currentUrl.match(/^https?:\/\/[^/]+/)[0]}/`
    : fallbackBaseUrl;

  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const location = message.location();
    if (location.url?.endsWith("/favicon.ico") && message.text().includes("404")) return;
    consoleErrors.push({ text: message.text(), location });
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400 && !response.url().endsWith("/favicon.ico")) {
      failedResponses.push({ status: response.status(), url: response.url() });
    }
  });
  page.on("request", (request) => {
    const url = request.url();
    if (url.includes("/api/runtime-execute")) {
      let body = null;
      try {
        body = request.postDataJSON();
      } catch (error) {
        salvageErrors.push(`execute request body unavailable: ${error.message}`);
      }
      executeRequests.push({ method: request.method(), url, body });
    }
    if (/^https?:/i.test(url) && !url.startsWith(baseUrl)) browserExternalRequests.push(url);
  });

  const dismissCompatibilitySplash = async () => {
    const splash = page.locator("#splash");
    if (!(await splash.count())) return;
    assert(!(await splash.isVisible()), "compatibility splash became visible");
    await splash.waitFor({ state: "detached", timeout: 5000 });
  };
  const captureScreenshot = async (name, required = false) => {
    const path = `${artifactDir}/${name}.png`;
    try {
      await page.screenshot({ path, fullPage: false });
      screenshots.push(path);
      return path;
    } catch (error) {
      const message = `screenshot ${name} unavailable: ${error.message}`;
      if (required) throw new Error(message);
      salvageErrors.push(message);
      return null;
    }
  };
  const sha256 = async (value) => page.evaluate(async (text) => {
    const bytes = new TextEncoder().encode(text);
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }, String(value || ""));
  const downloadJson = async (path, payload) => {
    const downloadPromise = page.waitForEvent("download");
    await page.evaluate((content) => {
      const link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob([content], { type: "application/json" }));
      link.download = "v81f-runtime-evidence.json";
      link.click();
    }, JSON.stringify(payload, null, 2));
    const download = await downloadPromise;
    await download.saveAs(path);
  };
  const updateSentry = async (patch) => {
    try {
      return await page.evaluate(({ key, update }) => {
        const raw = localStorage.getItem(key);
        const current = raw ? JSON.parse(raw) : {};
        const next = { ...current, ...update };
        localStorage.setItem(key, JSON.stringify(next));
        return next;
      }, { key: oneShotSentryKey, update: patch });
    } catch (error) {
      salvageErrors.push(`persistent sentry update failed: ${error.message}`);
      return null;
    }
  };
  const safeStep = async (label, action, fallback = null) => {
    try {
      return await action();
    } catch (error) {
      salvageErrors.push(`${label}: ${error.message}`);
      return fallback;
    }
  };
  const countDelta = (before, after) => Object.fromEntries(
    Object.keys(expectedSuccessLedgerDelta).map((key) => [
      key,
      Number(after?.[key] ?? 0) - Number(before?.[key] ?? 0),
    ]),
  );

  // Everything above the explicit click is fail-fast and cannot call the provider.
  const statusBeforeResponse = await page.request.get(`${baseUrl}api/status`);
  const statusBefore = await statusBeforeResponse.json();
  assert(statusBeforeResponse.ok(), "formal Runtime status is unavailable");
  assert(statusBefore.service_mode === "runtime-admin", "service is not formal runtime-admin");
  assert(statusBefore.runtime_admin === true && statusBefore.runtime_mock === false, "formal authority mode drifted");
  assert(statusBefore.execution_authority_present === true && statusBefore.can_execute === true, "execution authority is absent");
  assert(statusBefore.readonly_source_tables === true, "formal source tables are not read-only");
  assert(statusBefore.runtime_sidecar?.ready === true, "production Runtime sidecar is not ready");

  await page.setViewportSize({ width: 1707, height: 1067 });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await dismissCompatibilitySplash();
  const existingSentry = await page.evaluate((key) => localStorage.getItem(key), oneShotSentryKey);
  assert(!existingSentry, "persistent one-shot sentry already exists; DO NOT EXECUTE OR RERUN");
  await page.evaluate((session) => {
    sessionStorage.removeItem(session);
    localStorage.setItem("yunos.homeOmnibox.mode", "search");
    performance.clearResourceTimings();
  }, sessionKey);
  await page.reload({ waitUntil: "domcontentloaded" });
  await dismissCompatibilitySplash();

  await page.locator('[data-omnibox-mode="ai"]').click();
  await page.locator("#home-omnibox-input").fill(question);
  const previewResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/runtime-preview?"),
  );
  await page.locator("#home-omnibox-input").press("Enter");
  const previewResponse = await previewResponsePromise;
  const preview = await previewResponse.json();
  const binding = preview.reviewed_execution_binding || {};
  const reviewed = binding.reviewed_artifact || {};
  const profileBinding = reviewed.approved_profile || {};
  const evidence = Array.isArray(reviewed.evidence) ? reviewed.evidence : [];
  assert(previewResponse.ok() && preview.status === "preview", "review-only preview did not become ready");
  assert(preview.execution?.persisted === false, "preview persisted data");
  assert(preview.execution?.credential_read === false, "preview read a credential");
  assert(preview.execution?.external_model_calls === false, "preview called the provider");
  assert(preview.execution?.network_request_count === 0, "preview crossed the network boundary");
  assert(preview.execution?.automatic_retry === false, "preview enabled automatic retry");
  assert(preview.review?.evidence_reference_ids?.length > 0, "preview has no historical evidence");
  assert(preview.review?.approved_profile_included > 0, "preview has no approved profile items");
  assert(!("payload" in (preview.request_artifact || {})), "public preview leaked the sealed wire payload");
  assert(JSON.stringify(preview.runtime_profile) === JSON.stringify(expectedRuntimeProfile), "fixed Runtime profile drifted");
  assert(JSON.stringify(reviewed.runtime_profile) === JSON.stringify(expectedRuntimeProfile), "Reviewed Artifact Runtime profile drifted");
  assert(reviewed.question?.text === question, "Reviewed Artifact question drifted");
  assert(binding.request_id === preview.request_id && binding.mutation_id === preview.mutation_id, "Reviewed Binding request identity drifted");
  assert(binding.identity?.startsWith("reb_") && reviewed.identity?.startsWith("rva_"), "Reviewed identities are missing");
  assert(profileBinding.projection_identity?.startsWith("app_"), "approved profile projection identity is missing");
  assert(Number.isInteger(profileBinding.revision), "approved profile revision is missing");
  assert(JSON.stringify(profileBinding.profile_item_ids) === JSON.stringify(preview.review.profile_item_ids), "approved profile IDs drifted");
  assert(JSON.stringify(evidence.map((item) => item.evidence_reference_id)) === JSON.stringify(preview.review.evidence_reference_ids), "evidence IDs drifted");
  assert(evidence.every((item) => item.source_anchor && typeof item.source_anchor === "object"), "evidence source anchors are missing");
  await page.locator('#home-ai-document[data-ai-state="preview-ready"]').waitFor({ state: "visible" });
  const executeButton = page.locator("#home-ai-execute");
  assert(!(await executeButton.isDisabled()), "formal execution button is disabled");
  assert(executeRequests.length === 0, "execute POST occurred before the explicit click");
  await captureScreenshot("real-deepseek-preview-1707x1067", true);

  await page.evaluate(({ key, sentry }) => {
    localStorage.setItem(key, JSON.stringify(sentry));
    const button = document.querySelector("#home-ai-execute");
    if (!button) throw new Error("execute button missing while arming one-shot sentry");
    window.__yunosV81fRealExecuteClickCount = 0;
    button.addEventListener("click", () => {
      window.__yunosV81fRealExecuteClickCount += 1;
      const current = JSON.parse(localStorage.getItem(key) || "{}");
      localStorage.setItem(key, JSON.stringify({
        ...current,
        state: "CLICK_DISPATCHED_DO_NOT_RERUN",
        clickEvents: window.__yunosV81fRealExecuteClickCount,
        clickedAt: new Date().toISOString(),
        doNotRerun: true,
      }));
    }, { capture: true });
  }, {
    key: oneShotSentryKey,
    sentry: {
      version: "v8.1-f-real-deepseek-one-shot-sentry-1",
      state: "ARMED_BEFORE_CLICK",
      armedAt: new Date().toISOString(),
      doNotAutoClear: true,
      doNotRerunAfterClick: true,
      requestId: preview.request_id,
      mutationId: preview.mutation_id,
      reviewedBindingIdentity: binding.identity,
      reviewedArtifactIdentity: reviewed.identity,
      artifactIdentity: preview.request_artifact?.artifact_identity,
      modelRunId: preview.diagnostic_bindings?.model_run_id,
      runtimeLedgerBefore: statusBefore.runtime_sidecar?.counts,
    },
  });
  const armedSentry = JSON.parse(await page.evaluate((key) => localStorage.getItem(key), oneShotSentryKey));
  await downloadJson(sentryArtifact, armedSentry);

  const executeResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/runtime-execute"),
    { timeout: 180000 },
  );
  let clickDispatchError = null;
  try {
    // This is the one and only user-action click authorized for the formal provider call.
    await executeButton.click();
  } catch (error) {
    clickDispatchError = error.message;
  }
  const clickObservation = await safeStep("read explicit click observation", async () => ({
    eventCount: await page.evaluate(() => window.__yunosV81fRealExecuteClickCount || 0),
    sentry: JSON.parse(await page.evaluate((key) => localStorage.getItem(key), oneShotSentryKey)),
  }), { eventCount: 0, sentry: null });
  const clickBoundaryCrossed = Boolean(
    clickObservation.eventCount > 0
      || clickObservation.sentry?.state === "CLICK_DISPATCHED_DO_NOT_RERUN"
      || executeRequests.length > 0
  );
  if (!clickBoundaryCrossed) {
    violations.push("explicit click dispatch could not be proven; provider boundary is unknown");
    salvageErrors.push(`CALL_DISPATCH_UNKNOWN_DO_NOT_RERUN: ${clickDispatchError || "no observable click boundary"}`);
  }

  // From this point on, no exception may trigger a rerun. Every failure is salvaged
  // into localStorage/the evidence artifact and ends with DO_NOT_RERUN.
  if (clickDispatchError) salvageErrors.push(`execute click returned an error after dispatch: ${clickDispatchError}`);
  await safeStep("wait for executing state", () => page.locator(
    '#home-ai-document[data-ai-state="executing"]',
  ).waitFor({ state: "visible" }));
  await captureScreenshot("real-deepseek-executing-1707x1067");

  let executeHttpStatus = null;
  let envelope = null;
  const executeResponse = await safeStep("execute response unavailable", () => executeResponsePromise);
  if (executeResponse) {
    executeHttpStatus = executeResponse.status();
    envelope = await safeStep("execute response JSON unavailable", () => executeResponse.json());
  }

  let receivingObserved = false;
  const receiving = await safeStep("receiving-result state was not observable", async () => {
    await page.locator('#home-ai-document[data-ai-state="receiving-result"]').waitFor({
      state: "visible",
      timeout: 10000,
    });
    return true;
  }, false);
  if (receiving) {
    receivingObserved = true;
    await captureScreenshot("real-deepseek-receiving-result-1707x1067");
  }

  let terminalState = "unknown";
  await safeStep("terminal UI state unavailable", async () => {
    await page.waitForFunction(() => {
      const state = document.querySelector("#home-ai-document")?.dataset.aiState;
      return ["success", "truncated", "provider-error"].includes(state);
    }, undefined, { timeout: 180000 });
    terminalState = await page.locator("#home-ai-document").getAttribute("data-ai-state") || "unknown";
  });
  await captureScreenshot(`real-deepseek-terminal-${terminalState}-1707x1067`);

  const requestBody = executeRequests[0]?.body || {};
  const result = envelope?.result || {};
  const transport = envelope?.transport || {};
  const targetRunId = result.model_run_id || preview.diagnostic_bindings?.model_run_id;
  check(clickObservation.eventCount === 1, `measured execute click count is ${clickObservation.eventCount}, expected 1`);
  check(executeRequests.length === 1, `formal execute POST count is ${executeRequests.length}, expected 1`);
  check(executeRequests.every((item) => item.method === "POST"), "formal execute request was not POST");
  check(Object.keys(requestBody).sort().join("|") === ["budget", "confirmed", "expected_bindings", "mutation_id", "q", "request_id"].sort().join("|"), "formal request fields drifted");
  check(requestBody.q === preview.question && requestBody.budget === preview.budget_profile, "question or budget drifted after review");
  check(requestBody.request_id === preview.request_id && requestBody.mutation_id === preview.mutation_id, "request identity drifted after review");
  check(requestBody.confirmed === true, "explicit confirmation flag is missing");
  check(JSON.stringify(requestBody.expected_bindings) === JSON.stringify(binding), "Reviewed Binding drifted");
  check(!("max_tokens" in requestBody), "frontend attempted to override max_tokens");
  check(executeHttpStatus === 200, `formal execute HTTP status is ${executeHttpStatus}`);
  check(envelope?.status === "succeeded" && result.status === "succeeded", `formal Runtime status is ${envelope?.status || result.status || "missing"}`);
  check(terminalState === "success", `formal UI terminal state is ${terminalState}`);
  check(transport.exactly_one_call === true, "transport did not prove exactly one call");
  check(transport.adapter_constructor_count_this_request === 1, "adapter constructor count is not one");
  check(transport.adapter_call_count_this_request === 1, "adapter call count is not one");
  check(transport.model_call_count_for_run === 1, "model call count is not one");
  check(transport.automatic_retry === false, "automatic retry was enabled");
  check(transport.mock_transport === false, "formal execution used mock transport");
  check(transport.network_request_count === 1, "formal provider network request count is not one");
  check(transport.credential_read === true, "formal adapter did not report the credential read boundary");
  check(result.execution?.external_model_calls === true, "Runtime did not record an external model call");
  check(result.execution?.sent_to_model === true, "Runtime did not record sent_to_model");
  check(targetRunId === preview.diagnostic_bindings?.model_run_id, "run identity drifted");
  check(result.request_id === preview.request_id && result.mutation_id === preview.mutation_id, "result request identity drifted");
  check(result.request_artifact?.artifact_identity === preview.request_artifact?.artifact_identity, "artifact identity drifted");
  check(result.delivery_identity === preview.delivery_identity, "delivery identity drifted");
  check(Boolean(result.receipt_identity), "receipt identity is missing");
  check(Number.isInteger(result.usage?.prompt_tokens) && result.usage.prompt_tokens > 0, "prompt token usage is missing");
  check(Number.isInteger(result.usage?.completion_tokens) && result.usage.completion_tokens > 0, "completion token usage is missing");
  check(result.usage?.total_tokens === result.usage?.prompt_tokens + result.usage?.completion_tokens, "total token usage is inconsistent");
  check(typeof result.response?.finish_reason === "string" && result.response.finish_reason.length > 0, "finish_reason is missing");
  check(result.response?.finish_reason !== "length", "formal answer was truncated despite the short acceptance question");

  let runReadback = null;
  if (targetRunId) {
    const runResponse = await safeStep("runtime-run request unavailable", () => page.request.get(
      `${baseUrl}api/runtime-run?model_run_id=${encodeURIComponent(targetRunId)}`,
    ));
    if (runResponse) {
      if (runResponse.ok()) runReadback = await safeStep("runtime-run JSON unavailable", () => runResponse.json());
      else violations.push(`runtime-run readback failed with ${runResponse.status()}`);
    }
  } else {
    violations.push("model run identity is unavailable for ledger readback");
  }
  check(runReadback?.status === result.status, "ledger readback status drifted");
  check(runReadback?.request_id === preview.request_id && runReadback?.mutation_id === preview.mutation_id, "ledger request identity drifted");
  check(runReadback?.receipt_identity === result.receipt_identity, "ledger readback receipt drifted");
  check(runReadback?.request_artifact?.artifact_identity === result.request_artifact?.artifact_identity, "ledger readback artifact drifted");
  check(runReadback?.delivery_identity === result.delivery_identity, "ledger readback delivery drifted");
  check(JSON.stringify(runReadback?.usage) === JSON.stringify(result.usage), "ledger usage drifted");
  check(runReadback?.response?.finish_reason === result.response?.finish_reason, "ledger finish_reason drifted");

  let refresh = { restored: false, runtimeRunReads: 0, previewReads: 0, executePosts: 0 };
  if (["success", "truncated"].includes(terminalState)) {
    const recovered = await safeStep("refresh recovery unavailable", async () => {
      await page.evaluate(() => performance.clearResourceTimings());
      const executeCountBeforeReload = executeRequests.length;
      await page.reload({ waitUntil: "domcontentloaded" });
      await dismissCompatibilitySplash();
      await page.waitForFunction(() => {
        const state = document.querySelector("#home-ai-document")?.dataset.aiState;
        return ["success", "truncated"].includes(state);
      }, undefined, { timeout: 30000 });
      const resources = await page.evaluate(() => performance.getEntriesByType("resource").map((item) => item.name));
      return {
        restored: true,
        runtimeRunReads: resources.filter((name) => name.includes("/api/runtime-run?")).length,
        previewReads: resources.filter((name) => name.includes("/api/runtime-preview?")).length,
        executePosts: executeRequests.length - executeCountBeforeReload,
      };
    });
    if (recovered) {
      refresh = recovered;
      await captureScreenshot("real-deepseek-refresh-recovered-1707x1067");
      check(refresh.runtimeRunReads === 1, "refresh did not read Runtime ledger exactly once");
      check(refresh.previewReads === 1, "refresh did not rebuild preview exactly once");
      check(refresh.executePosts === 0, "refresh repeated execute POST");
      const statusText = await safeStep("refresh status text unavailable", () => page.locator("#home-ai-document-status").innerText(), "");
      check(statusText.includes("Runtime ledger"), "refresh result is not ledger-backed");
    }
  }

  const statusAfterPayload = await safeStep("status-after readback unavailable", async () => {
    const response = await page.request.get(`${baseUrl}api/status`);
    const body = await response.json();
    return { ok: response.ok(), status: response.status(), body };
  });
  check(statusAfterPayload?.ok === true, `status-after HTTP status is ${statusAfterPayload?.status ?? "missing"}`);
  const statusAfter = statusAfterPayload?.body || null;
  const ledgerDelta = countDelta(statusBefore.runtime_sidecar?.counts, statusAfter?.runtime_sidecar?.counts);
  if (result.status === "succeeded") {
    for (const [key, expected] of Object.entries(expectedSuccessLedgerDelta)) {
      check(ledgerDelta[key] === expected, `Runtime ledger ${key} delta is ${ledgerDelta[key]}, expected ${expected}`);
    }
  }

  check(browserExternalRequests.length === 0, `browser made unexpected external requests: ${JSON.stringify(browserExternalRequests)}`);
  check(consoleErrors.length === 0, `console errors: ${JSON.stringify(consoleErrors)}`);
  check(pageErrors.length === 0, `page errors: ${JSON.stringify(pageErrors)}`);
  check(failedResponses.length === 0, `failed responses: ${JSON.stringify(failedResponses)}`);

  const responseContent = result.response?.content || runReadback?.response?.content || "";
  const responseContentSha256 = await safeStep(
    "response hash unavailable", () => sha256(responseContent), null,
  );
  const evidenceClean = violations.length === 0 && salvageErrors.length === 0;
  const terminalDisposition = !clickBoundaryCrossed
    ? "CALL_DISPATCH_UNKNOWN_DO_NOT_RERUN"
    : (result.status === "succeeded" && evidenceClean
      ? "FORMAL_CALL_ACCEPTED_DO_NOT_RERUN"
      : (result.status === "failed"
        ? "PROVIDER_TERMINAL_FAILED_DO_NOT_RERUN"
        : "CALL_SENT_OR_IN_DOUBT_DO_NOT_RERUN"));
  await updateSentry({
    state: terminalDisposition,
    finalizedAt: new Date().toISOString(),
    clickEvents: clickObservation.eventCount,
    executePostCount: executeRequests.length,
    modelRunId: targetRunId,
    receiptIdentity: result.receipt_identity || runReadback?.receipt_identity || null,
    doNotRerun: true,
  });

  const report = {
    generatedAt: new Date().toISOString(),
    baseUrl,
    question,
    verdict: evidenceClean ? "FORMAL_CALL_ACCEPTED" : "NO_GO_DO_NOT_RERUN",
    disposition: terminalDisposition,
    mustNotRerun: true,
    authority: {
      serviceMode: statusBefore.service_mode,
      runtimeAdmin: statusBefore.runtime_admin,
      runtimeMock: statusBefore.runtime_mock,
      executionAuthorityPresent: statusBefore.execution_authority_present,
      canExecute: statusBefore.can_execute,
      readonlySourceTables: statusBefore.readonly_source_tables,
      boundEndpoint: statusBefore.bound_endpoint,
      processId: statusBefore.process_id,
      serverInstanceId: statusBefore.server_instance_id,
      runtimeSidecarStatus: statusBefore.runtime_sidecar?.status,
      runtimeSidecarReason: statusBefore.runtime_sidecar?.reason_code,
      runtimeSidecarIdentity: statusBefore.runtime_sidecar?.sidecar?.runtime_profile_identity,
      runtimeSidecarIntegrity: statusBefore.runtime_sidecar?.integrity,
    },
    reviewOnlyPreview: {
      status: preview.status,
      requestId: preview.request_id,
      mutationId: preview.mutation_id,
      evidenceCount: preview.review?.evidence_reference_ids?.length,
      approvedProfileCount: preview.review?.approved_profile_included,
      estimatedTokens: preview.review?.estimated_tokens,
      reviewedBindingIdentity: binding.identity,
      reviewedBindingVersion: binding.version,
      reviewedArtifactIdentity: reviewed.identity,
      reviewedArtifactVersion: reviewed.version,
      idempotencyFingerprint: binding.idempotency_fingerprint,
      artifactIdentity: preview.request_artifact?.artifact_identity,
      runIdentity: preview.diagnostic_bindings?.model_run_id,
      deliveryIdentity: preview.delivery_identity,
      assemblyIdentity: preview.delivery?.assembly_binding?.assembly_identity,
      modelContextSha256: reviewed.model_context?.sha256,
      wireBodySha256: reviewed.wire?.sha256,
      approvedProfile: {
        projectionIdentity: profileBinding.projection_identity,
        revision: profileBinding.revision,
        decisionCount: profileBinding.decision_count,
        profileItemIds: profileBinding.profile_item_ids,
      },
      evidence: evidence.map((item) => ({
        evidenceReferenceId: item.evidence_reference_id,
        sourceAnchor: item.source_anchor,
        excerptSha256: item.excerpt_sha256,
      })),
      runtimeProfile: preview.runtime_profile,
      fixedGenerationConfig: expectedGenerationConfig,
      sealedPayloadPubliclyExposed: "payload" in (preview.request_artifact || {}),
      execution: preview.execution,
    },
    explicitUserAction: {
      selector: "#home-ai-execute",
      measuredClickEventCount: clickObservation.eventCount,
      clickBoundaryCrossed,
      persistentSentryKey: oneShotSentryKey,
      externalSentryArtifact: sentryArtifact,
      sentinelAutoCleared: false,
    },
    execute: {
      httpStatus: executeHttpStatus,
      postCount: executeRequests.length,
      requestFields: Object.keys(requestBody).sort(),
      requestId: result.request_id || preview.request_id,
      mutationId: result.mutation_id || preview.mutation_id,
      modelRunId: targetRunId,
      artifactIdentity: result.request_artifact?.artifact_identity || preview.request_artifact?.artifact_identity,
      receiptIdentity: result.receipt_identity || runReadback?.receipt_identity || null,
      reviewedBindingIdentity: binding.identity,
      reviewedArtifactIdentity: reviewed.identity,
      deliveryIdentity: result.delivery_identity || preview.delivery_identity,
      usage: result.usage || runReadback?.usage || null,
      finishReason: result.response?.finish_reason || runReadback?.response?.finish_reason || null,
      responseContentCharacters: responseContent.length,
      responseContentSha256,
      terminalState,
      receivingObserved,
      transport,
      execution: result.execution || runReadback?.execution || null,
      automaticRetryCount: transport.automatic_retry === false ? 0 : null,
    },
    exactlyOnceEvidence: {
      frontendExecutePostCount: executeRequests.length,
      measuredClickEventCount: clickObservation.eventCount,
      adapterConstructorCount: transport.adapter_constructor_count_this_request,
      adapterCallCount: transport.adapter_call_count_this_request,
      modelCallCountForRun: transport.model_call_count_for_run,
      providerNetworkRequestCount: transport.network_request_count,
      automaticRetry: transport.automatic_retry,
      expectedRuntimeEventSequence: expectedSuccessEvents,
      directRuntimeEventReadbackArtifactRequired: true,
      physicalHttpBoundaryNote: "Runtime proves one adapter.send/urlopen boundary and no automatic retry; redirects are not independently metered in the append-only ledger.",
    },
    runReadback: runReadback ? {
      modelRunId: runReadback.model_run_id,
      requestId: runReadback.request_id,
      mutationId: runReadback.mutation_id,
      status: runReadback.status,
      executionPhase: runReadback.execution_phase,
      artifactIdentity: runReadback.request_artifact?.artifact_identity,
      receiptIdentity: runReadback.receipt_identity,
      deliveryIdentity: runReadback.delivery_identity,
      usage: runReadback.usage,
      finishReason: runReadback.response?.finish_reason,
    } : null,
    runtimeLedgerBefore: statusBefore.runtime_sidecar?.counts,
    runtimeLedgerAfter: statusAfter?.runtime_sidecar?.counts || null,
    runtimeLedgerDelta: ledgerDelta,
    expectedSuccessLedgerDelta,
    refresh,
    browserExternalRequests,
    consoleErrors,
    pageErrors,
    failedResponses,
    violations,
    salvageErrors,
    screenshots,
  };

  await safeStep("persist recovery report in localStorage", () => page.evaluate(({ key, value }) => {
    localStorage.setItem(key, JSON.stringify(value));
    window.__yunosV81fRealDeepSeekAcceptanceReport = value;
  }, { key: recoveryReportKey, value: report }));
  await safeStep("save formal evidence artifact", () => downloadJson(reportArtifact, report));

  // Never throw after the click boundary: a thrown error could invite a second
  // provider attempt. The returned disposition and artifact are authoritative.
  return report;
}
