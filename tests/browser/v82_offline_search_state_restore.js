async (page, harness) => {
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const apiRequests = [];
  const consoleErrors = [];
  const pageErrors = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/")) {
      apiRequests.push({ method: request.method(), url: request.url() });
    }
  });
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto(harness.baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.body.dataset.yunosPrivateCacheState === "ready",
  );
  await page.evaluate(() => navigator.serviceWorker.ready);
  if (!await page.evaluate(() => Boolean(navigator.serviceWorker.controller))) {
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.body.dataset.yunosPrivateCacheState === "ready",
    );
  }

  const onlineSearchStart = apiRequests.length;
  await page.locator("#mobile-search-input").fill("记忆");
  await page.waitForTimeout(1150);
  await page.waitForFunction(
    () => !document.querySelector("#mobile-search-status")?.classList.contains("is-loading"),
  );
  const onlineSearchRequests = apiRequests.slice(onlineSearchStart).filter(
    (request) => request.url.includes("/api/search"),
  );
  assert(onlineSearchRequests.length >= 1, "online mobile search did not request the host");
  assert(
    onlineSearchRequests.every((request) => request.method === "GET"),
    "online search used a non-GET method",
  );

  const seeded = await page.evaluate(async () => {
    const cache = globalThis.yunosPrivateCache;
    await cache.clearPrivateCache();
    const baseTime = Date.UTC(2026, 6, 1, 8, 0, 0);
    for (let conversationIndex = 0; conversationIndex < 65; conversationIndex += 1) {
      const conversationId = `e3-offline-conversation-${String(conversationIndex).padStart(3, "0")}`;
      const messages = Array.from({ length: 5 }, (_, messageIndex) => {
        const messageId = `${conversationId}-message-${messageIndex + 1}`;
        const extraMatch = conversationIndex === 0 && messageIndex === 3;
        return {
          conversationId,
          messageId,
          role: messageIndex % 2 ? "assistant" : "user",
          text: messageIndex === 2 || extraMatch
            ? `第 ${conversationIndex} 组离线缓存 验证 Offline Search 正文 ${messageIndex}`
            : `第 ${conversationIndex} 组上下文消息 ${messageIndex}`,
          messageTime: new Date(baseTime + conversationIndex * 60_000 + messageIndex).toISOString(),
          position: messageIndex + 1,
        };
      });
      await cache.saveMessageWindow({
        conversationId,
        conversationTitle: `缓存验证会话 ${conversationIndex}`,
        anchorMessageId: messages[2].messageId,
        direction: "preview",
        expectedMessageCount: 5,
        reachedStart: true,
        reachedEnd: true,
        messages,
      });
    }
    await cache.putRecord("messages", {
      conversationId: "e3-orphan",
      messageId: "e3-orphan-message",
      role: "user",
      text: "离线缓存 验证 孤立消息不得出现",
      contentVersion: 1,
    });
    await cache.putRecord("segments", {
      segmentKey: "e3-partial-segment",
      conversationId: "e3-partial",
      messageIds: ["e3-partial-message"],
      expectedMessageCount: 2,
      partial: true,
      missingMessageIds: ["e3-missing"],
      contentVersion: 1,
    });
    const inspection = await cache.inspect();
    return {
      counts: inspection.stats.categoryCounts,
      bytes: inspection.stats.categoryBytes,
    };
  });
  assert(seeded.counts.conversations === 65, "synthetic cache conversation count differs");
  assert(seeded.counts.messages === 326, "synthetic cache message count differs");
  assert(seeded.counts.segments === 66, "synthetic cache segment count differs");
  assert(seeded.counts.mediaObjects === 0 && seeded.counts.thumbnails === 0, "media stores are not empty");

  const core = await page.evaluate(async () => {
    const cache = globalThis.yunosPrivateCache;
    const normalized = await cache.searchCachedMessages({
      query: "  缓存   验证  ",
      sortMode: "relevance",
      limit: 20,
    });
    const mixed = await cache.searchCachedMessages({
      query: "offline SEARCH",
      sortMode: "relevance",
      limit: 20,
    });
    const stable = await cache.searchCachedMessages({
      query: "缓存 验证",
      sortMode: "relevance",
      limit: 20,
    });
    const stableAgain = await cache.searchCachedMessages({
      query: "缓存 验证",
      sortMode: "relevance",
      limit: 20,
    });
    const latest = await cache.searchCachedMessages({
      query: "缓存 验证",
      sortMode: "latest",
      limit: 20,
    });
    const snapshot = await cache.saveSearchSnapshot({
      query: "缓存 验证",
      sortMode: "relevance",
      resultRefs: stable.resultRefs,
    });
    const inspection = await cache.inspect();
    return {
      normalized: {
        normalizedQuery: normalized.normalizedQuery,
        itemCount: normalized.items.length,
        totalConversations: normalized.totalConversations,
        totalMessages: normalized.totalMessages,
        hasMore: normalized.hasMore,
        cachedMultiMatches: normalized.items.find(
          (item) => item.conversation_id === "e3-offline-conversation-000",
        )?.other_match_count,
        sourceMode: normalized.items[0]?.best_hit?.source_mode,
        completePreview: normalized.items[0]?.best_hit?.complete_preview,
      },
      mixedCount: mixed.totalConversations,
      stableRefs: stable.resultRefs,
      stableAgainRefs: stableAgain.resultRefs,
      latestFirst: latest.items[0]?.conversation_id,
      snapshot,
      snapshotContainsText: JSON.stringify(snapshot).includes('"text"'),
      counts: inspection.stats.categoryCounts,
      bytes: inspection.stats.categoryBytes,
    };
  });
  assert(core.normalized.normalizedQuery === "缓存 验证", "whitespace normalization differs");
  assert(core.normalized.itemCount === 20, "offline first page is not 20 conversations");
  assert(core.normalized.totalConversations === 65, "offline conversation aggregation differs");
  assert(core.normalized.totalMessages === 66, "offline message deduplication differs");
  assert(core.normalized.hasMore, "offline first page did not expose continuation");
  assert(core.normalized.cachedMultiMatches === 1, "cached multi-match count differs");
  assert(core.normalized.sourceMode === "cached-only", "offline result source marker differs");
  assert(core.normalized.completePreview === true, "complete preview availability differs");
  assert(core.mixedCount === 65, "mixed English case query differs");
  assert(
    JSON.stringify(core.stableRefs) === JSON.stringify(core.stableAgainRefs),
    "relevance ordering is not deterministic",
  );
  assert(
    core.latestFirst === "e3-offline-conversation-064",
    "latest ordering did not use message time",
  );
  assert(core.snapshot.snapshotVersion === 1, "search snapshot version differs");
  assert(core.snapshot.sourceMode === "cached-only", "snapshot source differs");
  assert(!core.snapshotContainsText, "search snapshot duplicated message text");
  assert(core.counts.searchSnapshots === 1, "search snapshot count differs");
  assert(core.counts.mediaObjects === 0 && core.counts.thumbnails === 0, "media store count changed");

  const beforeMultiTab = await page.evaluate(
    () => globalThis.yunosPrivateCache.inspect(),
  );
  const secondPage = await page.context().newPage();
  secondPage.on("pageerror", (error) => pageErrors.push(`second tab: ${error.message}`));
  await secondPage.goto(harness.baseUrl, { waitUntil: "domcontentloaded" });
  await secondPage.waitForFunction(
    () => document.body.dataset.yunosPrivateCacheState === "ready",
  );
  await secondPage.waitForTimeout(250);
  const multiTabInspections = await Promise.all([
    page.evaluate(() => globalThis.yunosPrivateCache.inspect()),
    secondPage.evaluate(() => globalThis.yunosPrivateCache.inspect()),
  ]);
  const multiTabSamples = await Promise.all([
    page.evaluate(() => globalThis.yunosPrivateCache.getRecord(
      "messages",
      ["e3-offline-conversation-000", "e3-offline-conversation-000-message-3"],
    )),
    secondPage.evaluate(() => globalThis.yunosPrivateCache.getRecord(
      "messages",
      ["e3-offline-conversation-000", "e3-offline-conversation-000-message-3"],
    )),
  ]);
  const multiTabSearch = await Promise.all([
    page.evaluate(() => globalThis.yunosPrivateCache.searchCachedMessages({
      query: "缓存 验证",
      sortMode: "relevance",
      limit: 20,
    })),
    secondPage.evaluate(() => globalThis.yunosPrivateCache.searchCachedMessages({
      query: "缓存 验证",
      sortMode: "relevance",
      limit: 20,
    })),
  ]);
  await secondPage.close();
  assert(
    multiTabSearch.every((result) => result.totalConversations === 65),
    `simultaneous multi-tab cached search returned inconsistent results: ${
      multiTabSearch.map((result) => result.totalConversations).join(",")
    }; before=${beforeMultiTab.stats.categoryCounts.segments}; after=${
      multiTabInspections.map((result) => result.stats.categoryCounts.segments).join(",")
    }; coverage=${
      multiTabSearch.map((result) => JSON.stringify(result.coverage)).join("|")
    }; scanned=${
      multiTabSearch.map((result) => result.performance.scannedMessages).join(",")
    }; normalized=${
      multiTabSearch.map((result) => JSON.stringify(result.normalizedQuery)).join(",")
    }; samples=${
      multiTabSamples.map((record) => JSON.stringify(record?.text)).join("|")
    }`,
  );
  assert(
    JSON.stringify(multiTabSearch[0].resultRefs)
      === JSON.stringify(multiTabSearch[1].resultRefs),
    "simultaneous multi-tab cached search ordering differs",
  );

  const boundaryAndPerformance = await page.evaluate(async () => {
    const transactionDone = (transaction) => new Promise((resolve, reject) => {
      transaction.addEventListener("complete", resolve, { once: true });
      transaction.addEventListener("abort", () => reject(transaction.error), { once: true });
      transaction.addEventListener("error", () => reject(transaction.error), { once: true });
    });
    const deleteDb = (name) => new Promise((resolve, reject) => {
      const request = indexedDB.deleteDatabase(name);
      request.addEventListener("success", resolve, { once: true });
      request.addEventListener("error", () => reject(request.error), { once: true });
      request.addEventListener("blocked", () => reject(new Error(`blocked delete ${name}`)), { once: true });
    });
    const openRaw = (name) => new Promise((resolve, reject) => {
      const request = indexedDB.open(name);
      request.addEventListener("success", () => resolve(request.result), { once: true });
      request.addEventListener("error", () => reject(request.error), { once: true });
    });
    const seedRaw = async (name, count) => {
      const cache = YunOSPrivateCache.create({ dbName: name, ownerId: `${name}-seed` });
      await cache.open();
      cache.close();
      const database = await openRaw(name);
      const transaction = database.transaction(
        ["conversations", "messages", "segments"],
        "readwrite",
      );
      const conversations = transaction.objectStore("conversations");
      const messages = transaction.objectStore("messages");
      const segments = transaction.objectStore("segments");
      const at = Date.UTC(2026, 6, 2, 0, 0, 0);
      for (let index = 0; index < count; index += 1) {
        const conversationId = `${name}-conversation-${String(index).padStart(5, "0")}`;
        const messageId = `${name}-message-${String(index).padStart(5, "0")}`;
        conversations.put({
          conversationId,
          title: `性能会话 ${index}`,
          cachedAt: at,
          validatedAt: at,
          expiresAt: at + YunOSPrivateCache.constants.ttlMs,
          lastAccessedAt: at,
          contentVersion: 1,
          byteSize: 0,
        });
        messages.put({
          conversationId,
          messageId,
          role: "user",
          text: `性能命中 ${index} cached search benchmark`,
          messageTime: new Date(at + index).toISOString(),
          cachedAt: at,
          validatedAt: at,
          expiresAt: at + YunOSPrivateCache.constants.ttlMs,
          lastAccessedAt: at,
          contentVersion: 1,
          byteSize: 0,
        });
        segments.put({
          segmentKey: `${name}-segment-${String(index).padStart(5, "0")}`,
          conversationId,
          conversationTitle: `性能会话 ${index}`,
          messageIds: [messageId],
          expectedMessageCount: 1,
          partial: false,
          missingMessageIds: [],
          direction: "initial",
          cachedAt: at,
          validatedAt: at,
          expiresAt: at + YunOSPrivateCache.constants.ttlMs,
          lastAccessedAt: at,
          contentVersion: 1,
          byteSize: 0,
        });
      }
      await transactionDone(transaction);
      database.close();
      return cache;
    };

    const names = {
      state: `e3-state-${Date.now()}`,
      perf60: `e3-perf-60-${Date.now()}`,
      perf1000: `e3-perf-1000-${Date.now()}`,
      perf10000: `e3-perf-10000-${Date.now()}`,
      clearRace: `e3-clear-race-${Date.now()}`,
      resumeFailure: `e3-resume-failure-${Date.now()}`,
    };
    let fakeNow = Date.UTC(2026, 6, 3, 0, 0, 0);
    const stateCache = YunOSPrivateCache.create({
      dbName: names.state,
      ownerId: "e3-state-owner",
      now: () => fakeNow,
    });
    await stateCache.open();
    await stateCache.saveMessageWindow({
      conversationId: "state-conversation",
      conversationTitle: "状态会话",
      anchorMessageId: "state-message",
      direction: "initial",
      expectedMessageCount: 1,
      messages: [{
        conversationId: "state-conversation",
        messageId: "state-message",
        role: "user",
        text: "状态恢复命中",
        messageTime: "2026-07-03T00:00:00Z",
      }],
    });
    const baseState = {
      route: "results",
      sourceMode: "cached-only",
      query: "状态恢复",
      sortMode: "relevance",
      loadedResultRefs: [{
        conversationId: "state-conversation",
        messageId: "state-message",
      }],
      resultAnchorRef: {
        conversationId: "state-conversation",
        messageId: "state-message",
      },
      resultScrollTop: 240,
    };
    const saved = await stateCache.saveResumeState(baseState);
    const originalSavedAt = saved.state.savedAt;
    fakeNow = originalSavedAt + (23 * 60 + 59) * 60_000;
    const at2359 = await stateCache.loadResumeState();
    fakeNow = originalSavedAt + 24 * 60 * 60_000;
    const at2400 = await stateCache.loadResumeState();
    const preserved = await stateCache.saveResumeState(
      { ...at2400.state, savedAt: originalSavedAt },
      { advanceSavedAt: false },
    );
    fakeNow = originalSavedAt + (24 * 60 + 1) * 60_000;
    const at2401 = await stateCache.loadResumeState();
    fakeNow = Date.UTC(2026, 6, 3, 12, 0, 0);
    const incompatibleSaved = await stateCache.saveResumeState(baseState);
    const incompatibleDatabase = await openRaw(
      YunOSPrivateCache.constants.resumeDbName,
    );
    const incompatibleTransaction = incompatibleDatabase.transaction(
      "resumeStates",
      "readwrite",
    );
    incompatibleTransaction.objectStore("resumeStates").put({
      ...incompatibleSaved.state,
      stateVersion: 999,
    });
    await transactionDone(incompatibleTransaction);
    incompatibleDatabase.close();
    const incompatible = await stateCache.loadResumeState();
    const corruptSaved = await stateCache.saveResumeState(baseState);
    const corruptDatabase = await openRaw(YunOSPrivateCache.constants.resumeDbName);
    const corruptTransaction = corruptDatabase.transaction("resumeStates", "readwrite");
    corruptTransaction.objectStore("resumeStates").put({
      ...corruptSaved.state,
      savedAt: "not-a-timestamp",
    });
    await transactionDone(corruptTransaction);
    corruptDatabase.close();
    const corrupt = await stateCache.loadResumeState();
    fakeNow = Date.UTC(2026, 6, 4, 0, 0, 0);
    const rollbackSaved = await stateCache.saveResumeState(baseState);
    fakeNow = rollbackSaved.state.savedAt - 10 * 60_000;
    const rollback = await stateCache.loadResumeState();
    fakeNow = Date.UTC(2026, 6, 5, 0, 0, 0);
    const readerSaved = await stateCache.saveResumeState({
      ...baseState,
      route: "reader",
      currentConversationId: "state-conversation",
      currentAnchorMessageId: "state-message",
      readerScrollAnchorMessageId: "state-message",
      readerScrollOffset: 31,
      topBarHidden: true,
    });
    const readerLoaded = await stateCache.loadResumeState();
    const clearResult = await stateCache.clearPrivateCache();
    stateCache.close();
    const failingResumeCache = YunOSPrivateCache.create({
      dbName: names.resumeFailure,
      ownerId: "e3-resume-migration-failure",
      now: () => fakeNow,
      resumeMigrationHook: () => {
        throw new Error("synthetic resume migration failure");
      },
    });
    await failingResumeCache.open();
    let resumeMigrationFailed = false;
    try {
      await failingResumeCache.saveResumeState(baseState);
    } catch (error) {
      resumeMigrationFailed = String(error?.message || error).includes(
        "synthetic resume migration failure",
      );
    }
    failingResumeCache.close();
    const databasesAfterResumeFailure = typeof indexedDB.databases === "function"
      ? await indexedDB.databases()
      : [];
    const resumeDatabaseRolledBack = typeof indexedDB.databases !== "function"
      || !databasesAfterResumeFailure.some(
        (database) => database.name === YunOSPrivateCache.constants.resumeDbName,
      );

    const performanceMetrics = {};
    for (const [label, name, count] of [
      ["messages60", names.perf60, 60],
      ["messages1000", names.perf1000, 1000],
      ["messages10000", names.perf10000, 10000],
    ]) {
      await seedRaw(name, count);
      const cache = YunOSPrivateCache.create({ dbName: name, ownerId: `${name}-search` });
      await cache.open();
      const result = await cache.searchCachedMessages({
        query: "性能命中",
        limit: 20,
        batchSize: 100,
      });
      performanceMetrics[label] = {
        durationMs: result.performance.durationMs,
        maxBatchMs: result.performance.maxBatchMs,
        scannedMessages: result.performance.scannedMessages,
        totalConversations: result.totalConversations,
      };
      cache.close();
    }

    const cancelCache = YunOSPrivateCache.create({
      dbName: names.perf10000,
      ownerId: "e3-cancel",
    });
    await cancelCache.open();
    const controller = new AbortController();
    const cancellationStarted = performance.now();
    const cancellationPromise = cancelCache.searchCachedMessages({
      query: "性能命中",
      signal: controller.signal,
      batchSize: 50,
    });
    setTimeout(() => controller.abort(), 0);
    let cancelled = false;
    try {
      await cancellationPromise;
    } catch (error) {
      cancelled = error?.name === "AbortError";
    }
    const cancellationMs = performance.now() - cancellationStarted;
    cancelCache.close();

    let lruDeleted = false;
    const lruCache = YunOSPrivateCache.create({
      dbName: names.perf60,
      ownerId: "e3-lru-race",
      yieldControl: async () => {
        if (!lruDeleted) {
          const database = await openRaw(names.perf60);
          const transaction = database.transaction("segments", "readwrite");
          transaction.objectStore("segments").delete(`${names.perf60}-segment-00000`);
          await transactionDone(transaction);
          database.close();
          lruDeleted = true;
        }
        await new Promise((resolve) => setTimeout(resolve, 0));
      },
    });
    await lruCache.open();
    const lruResult = await lruCache.searchCachedMessages({
      query: "性能命中",
      limit: 100,
      batchSize: 10,
    });
    lruCache.close();

    await seedRaw(names.clearRace, 60);
    let clearedDuringSearch = false;
    const clearRaceCache = YunOSPrivateCache.create({
      dbName: names.clearRace,
      ownerId: "e3-clear-race",
      yieldControl: async () => {
        if (!clearedDuringSearch) {
          const database = await openRaw(names.clearRace);
          const transaction = database.transaction("segments", "readwrite");
          transaction.objectStore("segments").clear();
          await transactionDone(transaction);
          database.close();
          clearedDuringSearch = true;
        }
        await new Promise((resolve) => setTimeout(resolve, 0));
      },
    });
    await clearRaceCache.open();
    const clearRaceResult = await clearRaceCache.searchCachedMessages({
      query: "性能命中",
      limit: 100,
      batchSize: 10,
    });
    clearRaceCache.close();

    for (const name of Object.values(names)) await deleteDb(name);
    return {
      boundary: {
        at2359: at2359.status,
        at2400: at2400.status,
        at2401: at2401.status,
        preservedSavedAt: preserved.state.savedAt,
        originalSavedAt,
        incompatible: incompatible.reason,
        corrupt: corrupt.reason,
        rollback: rollback.reason,
        reader: readerLoaded.status,
        readerOffset: readerLoaded.state?.readerScrollOffset,
        readerTopBarHidden: readerLoaded.state?.topBarHidden,
        clearResult,
        resumeMigrationFailed,
        resumeDatabaseRolledBack,
        readerSavedAt: readerSaved.state.savedAt,
      },
      performance: performanceMetrics,
      cancellation: { cancelled, cancellationMs },
      lru: {
        deleted: lruDeleted,
        totalConversations: lruResult.totalConversations,
        deletedRefPresent: lruResult.resultRefs.some(
          (ref) => ref.messageId === `${names.perf60}-message-00000`,
        ),
      },
      clearRace: {
        cleared: clearedDuringSearch,
        totalConversations: clearRaceResult.totalConversations,
      },
    };
  });
  assert(boundaryAndPerformance.boundary.at2359 === "ready", "23h59 restore failed");
  assert(boundaryAndPerformance.boundary.at2400 === "ready", "24h boundary restore failed");
  assert(boundaryAndPerformance.boundary.at2401 === "expired", "24h01 state did not expire");
  assert(
    boundaryAndPerformance.boundary.preservedSavedAt
      === boundaryAndPerformance.boundary.originalSavedAt,
    "restore without interaction extended savedAt",
  );
  assert(
    boundaryAndPerformance.boundary.rollback === "clock_rollback",
    "clock rollback did not fail closed",
  );
  assert(
    boundaryAndPerformance.boundary.incompatible === "incompatible_state_version",
    "incompatible resume state did not fail closed",
  );
  assert(
    boundaryAndPerformance.boundary.corrupt === "invalid_saved_at",
    "corrupt resume state did not fail closed",
  );
  assert(boundaryAndPerformance.boundary.reader === "ready", "reader resume validation failed");
  assert(boundaryAndPerformance.boundary.readerOffset === 31, "reader relative offset differs");
  assert(boundaryAndPerformance.boundary.readerTopBarHidden, "reader topbar state differs");
  assert(boundaryAndPerformance.boundary.clearResult.resumeCleared, "safe clear did not remove resume state");
  assert(
    boundaryAndPerformance.boundary.resumeMigrationFailed,
    "resume migration failure was not surfaced",
  );
  assert(
    boundaryAndPerformance.boundary.resumeDatabaseRolledBack,
    "failed resume database creation was not rolled back",
  );
  for (const [label, expected] of [
    ["messages60", 60],
    ["messages1000", 1000],
    ["messages10000", 10000],
  ]) {
    const metric = boundaryAndPerformance.performance[label];
    assert(metric.totalConversations === expected, `${label} result count differs`);
    assert(metric.scannedMessages === expected, `${label} scan count differs`);
  }
  assert(boundaryAndPerformance.cancellation.cancelled, "active cached search was not cancelled");
  assert(!boundaryAndPerformance.lru.deletedRefPresent, "LRU-deleted segment survived search revalidation");
  assert(boundaryAndPerformance.lru.totalConversations === 59, "LRU race count differs");
  assert(boundaryAndPerformance.clearRace.totalConversations === 0, "cache clear race kept stale results");

  await page.evaluate(async () => {
    const cache = globalThis.yunosPrivateCache;
    await cache.clearPrivateCache();
    const baseTime = Date.UTC(2026, 6, 1, 8, 0, 0);
    for (let conversationIndex = 0; conversationIndex < 65; conversationIndex += 1) {
      const conversationId = `e3-offline-conversation-${String(conversationIndex).padStart(3, "0")}`;
      const messages = Array.from({ length: 5 }, (_, messageIndex) => ({
        conversationId,
        messageId: `${conversationId}-message-${messageIndex + 1}`,
        role: messageIndex % 2 ? "assistant" : "user",
        text: messageIndex === 2 || (conversationIndex === 0 && messageIndex === 3)
          ? `第 ${conversationIndex} 组离线缓存 验证 Offline Search 正文 ${messageIndex}`
          : `第 ${conversationIndex} 组上下文消息 ${messageIndex}`,
        messageTime: new Date(baseTime + conversationIndex * 60_000 + messageIndex).toISOString(),
        position: messageIndex + 1,
      }));
      await cache.saveMessageWindow({
        conversationId,
        conversationTitle: `缓存验证会话 ${conversationIndex}`,
        anchorMessageId: messages[2].messageId,
        direction: "preview",
        expectedMessageCount: 5,
        reachedStart: true,
        reachedEnd: true,
        messages,
      });
    }
  });

  const offlineStatusStarted = Date.now();
  await page.context().setOffline(true);
  await page.waitForFunction(
    () => document.querySelector("#pwa-host-status")?.dataset.displayMode === "expanded",
  );
  assert(
    await page.locator("#pwa-host-status").isVisible(),
    "offline host status did not appear in expanded mode",
  );
  await page.waitForFunction(
    () => document.querySelector("#pwa-host-status")?.dataset.displayMode === "compact",
    null,
    { timeout: 5000 },
  );
  const offlineHostStatus = await page.locator("#pwa-host-status").evaluate((node) => {
    const box = node.getBoundingClientRect();
    const searchBox = document.querySelector("#mobile-search-form")?.getBoundingClientRect();
    return {
      displayMode: node.dataset.displayMode,
      width: box.width,
      height: box.height,
      rightGap: window.innerWidth - box.right,
      top: box.top,
      overlapsSearch: Boolean(searchBox) && !(
        box.right <= searchBox.left
        || box.left >= searchBox.right
        || box.bottom <= searchBox.top
        || box.top >= searchBox.bottom
      ),
    };
  });
  const offlineStatusElapsedMs = Date.now() - offlineStatusStarted;
  assert(offlineStatusElapsedMs >= 3100, "offline host status collapsed before its fade completed");
  assert(offlineStatusElapsedMs <= 3800, "offline host status stayed expanded beyond three seconds");
  assert(offlineHostStatus.displayMode === "compact", "offline host status did not become compact");
  assert(
    offlineHostStatus.width <= 9 && offlineHostStatus.height <= 9,
    "offline host status compact indicator is not a tiny dot",
  );
  assert(!offlineHostStatus.overlapsSearch, "offline host status dot overlaps the search UI");
  await harness.stopServer();
  const offlineSearchStart = apiRequests.length;
  await page.locator("#mobile-search-input").fill("缓存 验证");
  await page.waitForTimeout(1200);
  await page.waitForFunction(
    () => document.querySelectorAll("#mobile-search-results .yunos-mobile-result").length >= 20,
  );
  const offlineSearchRequests = apiRequests.slice(offlineSearchStart).filter(
    (request) => request.url.includes("/api/search"),
  );
  assert(offlineSearchRequests.length === 0, "offline search emitted /api/search");
  assert(
    await page.locator("#mobile-offline-search-banner").isVisible(),
    "cached-only banner is not visible",
  );
  assert(
    (await page.locator("#mobile-offline-search-banner").textContent()).includes("仅搜索已缓存内容"),
    "cached-only copy differs",
  );
  assert(
    (await page.locator("#mobile-offline-search-coverage").textContent()).includes("65 个缓存会话"),
    "cached coverage copy differs",
  );
  await page.screenshot({
    path: `${harness.artifactDir}/offline-host-status-compact.png`,
    animations: "disabled",
  });
  const multiResult = page.locator(
    '#mobile-search-results .yunos-mobile-result[data-conversation-id="e3-offline-conversation-000"]',
  );
  await multiResult.locator(".yunos-mobile-result-body").first().click();
  await multiResult.locator(".yunos-mobile-context-preview").first().waitFor({
    state: "visible",
  });
  await multiResult.locator(".yunos-mobile-preview-offline").first().waitFor({
    state: "visible",
  });
  assert(
    await multiResult.locator(".yunos-mobile-preview-offline").first().isVisible(),
    "cached preview did not expand",
  );
  const multiMatch = multiResult.locator(".yunos-mobile-more-matches");
  await multiMatch.click();
  await page.locator("#mobile-results-page").evaluate((node) => {
    node.scrollTop = 520;
    node.dispatchEvent(new Event("scroll"));
  });
  await page.evaluate(() => saveMobileRestoreState({ interaction: true }));
  const savedSearchState = await page.evaluate(async () => {
    const result = await globalThis.yunosPrivateCache.loadResumeState();
    return result.state;
  });
  assert(savedSearchState.route === "results", "search route was not saved");
  assert(savedSearchState.query === "缓存 验证", "search query was not saved");
  assert(savedSearchState.expandedConversationId, "expanded conversation was not saved");
  assert(savedSearchState.expandedPreviewRef?.messageId, "expanded preview was not saved");
  assert(savedSearchState.loadedResultRefs.length >= 20, "loaded result refs were not saved");

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.body.dataset.yunosPrivateCacheState === "ready",
  );
  await page.waitForFunction(
    () => document.querySelector("#mobile-search-input")?.value === "缓存 验证"
      && document.querySelectorAll("#mobile-search-results .yunos-mobile-result").length >= 20,
  );
  await page.waitForFunction(
    () => (document.querySelector("#mobile-results-page")?.scrollTop || 0) > 0,
  );
  const restoredSearch = await page.evaluate(() => ({
    query: document.querySelector("#mobile-search-input")?.value,
    sourceMode: document.querySelector("#mobile-offline-search-banner")?.hidden
      ? "online"
      : "cached-only",
    scrollTop: document.querySelector("#mobile-results-page")?.scrollTop,
    expanded: document.querySelector(
      ".yunos-mobile-more-matches[aria-expanded='true']",
    ) !== null,
    previewExpanded: document.querySelector(
      ".yunos-mobile-result-body[aria-expanded='true']",
    ) !== null,
  }));
  assert(restoredSearch.query === "缓存 验证", "offline query was not restored");
  assert(restoredSearch.scrollTop > 0, "offline result position was not restored");
  assert(restoredSearch.expanded, "offline multi-match expansion was not restored");
  assert(restoredSearch.previewExpanded, "offline preview expansion was not restored");

  await page.locator("#mobile-search-results .yunos-mobile-result-source").first().click();
  await page.waitForFunction(
    () => document.querySelectorAll("#mobile-reader-messages .yunos-mobile-reader-message").length === 5,
  );
  assert(
    await page.locator("#mobile-reader-offline-banner").isVisible(),
    "cached conversation banner is not visible",
  );
  await page.locator("#mobile-reader-hide-topbar").click();
  await page.locator("#mobile-reader-scroll").evaluate((node) => {
    node.scrollTop = Math.min(80, node.scrollHeight - node.clientHeight);
    node.dispatchEvent(new Event("scroll"));
  });
  await page.evaluate(() => saveMobileRestoreState({ interaction: true }));
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.body.dataset.yunosPrivateCacheState === "ready"
      && document.querySelector("#yunos-mobile-shell")?.dataset.mobileView === "reader"
      && document.querySelectorAll("#mobile-reader-messages .yunos-mobile-reader-message").length === 5,
  );
  const restoredReader = await page.evaluate(() => ({
    topbarHidden: document.querySelector("#mobile-reader-topbar")?.hidden,
    offlineBanner: !document.querySelector("#mobile-reader-offline-banner")?.hidden,
    title: document.querySelector("#mobile-reader-title")?.textContent,
    horizontalOverflow: Math.max(
      document.documentElement.scrollWidth - document.documentElement.clientWidth,
      document.body.scrollWidth - document.body.clientWidth,
    ),
  }));
  assert(restoredReader.topbarHidden, "reader topbar state was not restored");
  assert(restoredReader.offlineBanner, "reader did not restore as cached copy");
  assert(restoredReader.horizontalOverflow <= 1, "mobile reader has horizontal overflow");

  await page.locator("#mobile-reader-restore-topbar").click();
  await page.locator("#mobile-reader-back").click();
  await page.waitForFunction(
    () => document.querySelector("#yunos-mobile-shell")?.dataset.mobileView === "results",
  );
  await harness.startServer();
  await page.context().setOffline(false);
  await page.waitForTimeout(250);
  await page.waitForFunction(
    () => !document.querySelector("#mobile-search-all-host")?.hidden,
  );
  const recoveryStart = apiRequests.length;
  await page.locator("#mobile-search-all-host").click();
  await page.waitForFunction(
    () => !document.querySelector("#mobile-search-status")?.classList.contains("is-loading"),
  );
  const recoveryRequests = apiRequests.slice(recoveryStart).filter(
    (request) => request.url.includes("/api/search"),
  );
  assert(recoveryRequests.length >= 1, "host search recovery did not request /api/search");
  assert(
    recoveryRequests.every((request) => request.method === "GET"),
    "host recovery used a non-GET method",
  );
  assert(
    await page.locator("#mobile-offline-search-banner").isHidden(),
    "cached-only banner remained after successful host search",
  );
  assert(
    await page.locator("#pwa-host-status").isHidden(),
    "offline host status remained visible after host recovery",
  );
  assert(
    await page.locator("#pwa-host-status").getAttribute("data-display-mode") === "hidden",
    "offline host status did not reset after host recovery",
  );

  const cacheStorage = await page.evaluate(async () => {
    const entries = [];
    for (const cacheName of await caches.keys()) {
      const cache = await caches.open(cacheName);
      for (const request of await cache.keys()) {
        entries.push({ cacheName, pathname: new URL(request.url).pathname });
      }
    }
    return entries;
  });
  assert(
    cacheStorage.every((entry) => !entry.pathname.startsWith("/api/")),
    "CacheStorage contains an API response",
  );
  assert(
    apiRequests.every((request) => request.method === "GET"),
    "browser emitted a non-GET business request",
  );

  await page.screenshot({
    path: `${harness.artifactDir}/offline-search-restored.png`,
    animations: "disabled",
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.reload({ waitUntil: "networkidle" });
  assert(await page.locator("#yunos-desktop-shell").isVisible(), "desktop shell regressed");
  assert(!await page.locator("#yunos-mobile-shell").isVisible(), "mobile shell leaked on desktop");
  const desktopOverflow = await page.evaluate(() => Math.max(
    document.documentElement.scrollWidth - document.documentElement.clientWidth,
    document.body.scrollWidth - document.body.clientWidth,
  ));
  assert(desktopOverflow <= 1, "desktop page has horizontal overflow");

  const unexpectedConsoleErrors = consoleErrors.filter(
    (message) => !message.includes("ERR_INTERNET_DISCONNECTED")
      && !message.includes("ERR_FAILED"),
  );
  assert(pageErrors.length === 0, `page errors: ${pageErrors.join(" | ")}`);
  assert(
    unexpectedConsoleErrors.length === 0,
    `console errors: ${unexpectedConsoleErrors.join(" | ")}`,
  );
  return {
    status: "passed",
    onlineSearchRequests: onlineSearchRequests.length,
    offlineSearchRequests: offlineSearchRequests.length,
    recoverySearchRequests: recoveryRequests.length,
    apiMethods: [...new Set(apiRequests.map((request) => request.method))],
    seeded,
    core,
    boundaryAndPerformance,
    multiTabSearch: multiTabSearch.map((result) => ({
      totalConversations: result.totalConversations,
      firstRef: result.resultRefs[0],
    })),
    multiTabInspections: multiTabInspections.map(
      (inspection) => inspection.stats.categoryCounts,
    ),
    restoredSearch,
    restoredReader,
    offlineHostStatus: {
      ...offlineHostStatus,
      expandedDurationMs: offlineStatusElapsedMs,
    },
    cacheStorage,
    desktopOverflow,
    pageErrors,
    consoleErrors: unexpectedConsoleErrors,
  };
}
