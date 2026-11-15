async (page, harness = {}) => {
  const baseUrl = harness.baseUrl || "http://127.0.0.1:8786/";
  const cognitiveFixture = harness.cognitiveFixture;
  const consoleErrors = [];
  const pageErrors = [];
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  assert(cognitiveFixture?.agency_level === "act", "an authoritative Act fixture is required");

  page.setDefaultTimeout(15_000);
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("favicon.ico")) {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  const splash = page.locator("#splash");
  if (await splash.count() && await splash.isVisible()) await splash.click();

  const loaded = await page.evaluate(async () => {
    const bundle = await loadCognitiveWorkspaceBundle({
      surface: "desktop",
      allowLatest: true,
    });
    if (!bundle) throw new Error("COGNITIVE_TRUTH_FIXTURE_NOT_FOUND");
    homeAiState.cognitiveBundle = bundle;
    homeAiState.cognitiveBundleStatus = "ready";
    homeAiState.cognitiveBundleDetail = "";
    homeAiState.phase = HOME_AI_PHASES.PREPARING_PREVIEW;
    homeAiState.preview = null;
    homeAiState.envelope = null;
    setHomeOmniboxMode("ai");
    setShellPageState("ai");
    document.querySelector("#home-ai-document").hidden = false;
    renderCognitiveWorkspace({ phase: HOME_AI_PHASES.PREPARING_PREVIEW });
    return { surface: bundle.surface, runId: bundle.run?.run_id };
  });
  assert(loaded.surface === "desktop", "truth probe loaded the wrong surface");
  assert(loaded.runId === cognitiveFixture.run_id, "truth probe loaded the wrong run");

  const truth = await page.evaluate(() => {
    const bundle = homeAiState.cognitiveBundle;
    const planBefore = JSON.stringify(bundle.plan);
    const action = bundle.plan.nodes.find((node) => node.node_kind === "action");
    const renderedAction = Array.from(document.querySelectorAll(".yunos-cognitive-plan-node"))
      .find((node) => node.textContent.includes(action.title));
    const baselineModel = cognitiveWorkspaceModel(
      null,
      null,
      HOME_AI_PHASES.PREPARING_PREVIEW,
      bundle,
    );
    const activity = Date.parse(
      baselineModel.lastActivityAt || baselineModel.parts.checkpoint.last_activity_at,
    );
    const stalledBundle = structuredClone(bundle);
    stalledBundle.supervision.items.push({ event_type: "suspected_stall" });
    stalledBundle.supervision.count = stalledBundle.supervision.items.length;
    const stalledModel = cognitiveWorkspaceModel(
      null,
      null,
      HOME_AI_PHASES.PREPARING_PREVIEW,
      stalledBundle,
    );
    const originalBundle = homeAiState.cognitiveBundle;
    const riskValue = (risk) => {
      const clone = structuredClone(originalBundle);
      clone.objective.contract.risk_tolerance = risk;
      homeAiState.cognitiveBundle = clone;
      renderCognitiveWorkspace({ phase: HOME_AI_PHASES.PREPARING_PREVIEW });
      return Array.from(document.querySelectorAll(
        ".yunos-cognitive-top-summary .yunos-ai-summary-value",
      ))[5]?.textContent.trim();
    };
    const risks = {
      critical: riskValue("critical"),
      unknown: riskValue("unexpected_vendor_risk"),
    };
    homeAiState.cognitiveBundle = originalBundle;
    renderCognitiveWorkspace({ phase: HOME_AI_PHASES.PREPARING_PREVIEW });
    return {
      fixture: {
        risk: bundle.objective.contract.risk_tolerance,
        ceiling: bundle.authorization.risk_ceiling,
        runStatus: bundle.run.status,
        checkpointStatus: bundle.checkpoint.run_status,
        safeToResume: bundle.checkpoint.safe_to_resume,
        currentNodeIds: bundle.checkpoint.current_node_ids,
        completedNodeIds: bundle.checkpoint.completed_node_ids,
        supervision: bundle.supervision.items.map((event) => [
          event.event_type,
          event.severity,
          event.detail,
        ]),
      },
      overlay: {
        nodeId: action.node_id,
        originalState: action.state,
        renderedState: renderedAction?.dataset.planState,
        renderedSource: renderedAction?.dataset.planStateSource,
        planUnchanged: JSON.stringify(bundle.plan) === planBefore,
        digests: [
          bundle.plan.plan_digest,
          bundle.run.plan_digest,
          bundle.checkpoint.plan_digest,
          bundle.authorization.plan_digest,
        ],
      },
      risks,
      stall: {
        activity,
        beforeThreshold: cognitiveStallStatus(
          baselineModel,
          activity + COGNITIVE_STALL_THRESHOLD_MS - 1,
        ),
        atThreshold: cognitiveStallStatus(
          baselineModel,
          activity + COGNITIVE_STALL_THRESHOLD_MS,
        ),
        persisted: cognitiveStallStatus(stalledModel, activity + 1),
      },
    };
  });

  assert(truth.fixture.risk === "low" && truth.fixture.ceiling === "low", "risk binding drifted");
  assert(
    truth.fixture.runStatus === "waiting_acceptance"
      && truth.fixture.checkpointStatus === "waiting_acceptance",
    "acceptance boundary drifted",
  );
  assert(truth.fixture.safeToResume && truth.fixture.currentNodeIds.length === 0, (
    "checkpoint is not safely quiescent"
  ));
  assert(truth.fixture.completedNodeIds.includes(truth.overlay.nodeId), (
    "checkpoint omitted the completed action"
  ));
  assert(JSON.stringify(truth.fixture.supervision) === JSON.stringify([
    ["checkpoint", "info", "independent_verification_recorded"],
  ]), "fixture supervision drifted");
  assert(
    truth.overlay.originalState === "ready"
      && truth.overlay.renderedState === "completed"
      && truth.overlay.renderedSource === "checkpoint",
    "checkpoint overlay did not display the completed execution truth",
  );
  assert(
    truth.overlay.planUnchanged && new Set(truth.overlay.digests).size === 1,
    "rendering mutated the immutable Plan or drifted its digest binding",
  );
  assert(truth.risks.critical === "严重", "critical risk was not rendered as 严重");
  assert(
    truth.risks.unknown === "未识别风险：unexpected_vendor_risk",
    "unknown risk was silently normalized",
  );
  assert(Number.isFinite(truth.stall.activity), "fixture activity timestamp is invalid");
  assert(truth.stall.beforeThreshold.includes("仍在 20 分钟阈值内"), (
    "pre-threshold activity was not healthy"
  ));
  assert(truth.stall.atThreshold.includes("超过 20 分钟阈值"), (
    "the 20-minute threshold was not inclusive"
  ));
  assert(truth.stall.persisted.includes("已持久化 suspected_stall"), (
    "persisted suspected_stall did not take precedence"
  ));

  await page.screenshot({
    path: `${harness.artifactDir}/checkpoint-risk-stall-truth.png`,
    fullPage: true,
  });
  assert(consoleErrors.length === 0, `console errors: ${consoleErrors.join(" | ")}`);
  assert(pageErrors.length === 0, `page errors: ${pageErrors.join(" | ")}`);
  return { loaded, truth, consoleErrors, pageErrors };
}
