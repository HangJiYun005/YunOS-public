async (page, harness = {}) => {
  const baseUrl = harness.baseUrl || "http://127.0.0.1:8784/";
  const apiRequests = [];
  const consoleErrors = [];
  const pageErrors = [];
  const testDatabases = [];
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const waitUntil = async (predicate, message, timeout = 12000) => {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (await predicate()) return;
      await page.waitForTimeout(50);
    }
    throw new Error(message);
  };

  page.setDefaultTimeout(12000);
  await page.setViewportSize({ width: 390, height: 844 });
  page.on("request", (request) => {
    if (request.url().includes("/api/")) {
      apiRequests.push({ method: request.method(), url: request.url() });
    }
  });
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("favicon.ico")) {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.evaluate(async () => navigator.serviceWorker.ready);
  if (!await page.evaluate(() => Boolean(navigator.serviceWorker.controller))) {
    await page.reload({ waitUntil: "networkidle" });
  }
  await waitUntil(
    () => page.evaluate(() => document.body.dataset.yunosPrivateCacheState === "ready"),
    "private cache core did not initialize",
  );

  const initial = await page.evaluate(async () => {
    const response = await fetch("/api/status");
    const cacheEntries = [];
    for (const cacheName of await caches.keys()) {
      const cache = await caches.open(cacheName);
      for (const request of await cache.keys()) {
        cacheEntries.push({
          cacheName,
          pathname: new URL(request.url).pathname,
        });
      }
    }
    const defaultInspection = await globalThis.yunosPrivateCache.inspect();
    return {
      cacheControl: response.headers.get("cache-control"),
      cacheNames: await caches.keys(),
      cacheEntries,
      defaultInspection,
      mobileShellPresent: Boolean(document.querySelector("#yunos-mobile-shell")),
    };
  });
  assert(initial.cacheControl === "no-store", "private API response is not no-store");
  assert(initial.mobileShellPresent, "full mobile shell is absent");
  assert(
    initial.cacheNames.some((name) => name.startsWith("yunos-app-shell-v8.2-e3-")),
    "E1 application shell cache is absent",
  );
  assert(
    initial.cacheEntries.every((entry) => !entry.pathname.startsWith("/api/")),
    "API response entered CacheStorage",
  );
  assert(
    initial.defaultInspection.stats.totalBytes === 0,
    "mobile startup cached private content unexpectedly",
  );

  const core = await page.evaluate(async () => {
    const deleteDb = (name) => new Promise((resolve, reject) => {
      const request = indexedDB.deleteDatabase(name);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error(`delete blocked: ${name}`));
    });
    const openRaw = (name, version, upgrade = null) => new Promise((resolve, reject) => {
      const request = indexedDB.open(name, version);
      request.onupgradeneeded = () => upgrade?.(request.result, request.transaction);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const databaseNames = async () => (
      typeof indexedDB.databases === "function"
        ? (await indexedDB.databases()).map((item) => item.name)
        : []
    );
    const makeStorage = (state) => ({
      estimate: async () => ({ usage: state.usage, quota: state.quota }),
      persisted: async () => state.persisted,
      persist: async () => {
        state.persistCalls += 1;
        state.persisted = state.persistResult;
        return state.persistResult;
      },
    });
    const result = {};
    const prefix = `yunos-e1-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const names = {
      schema: `${prefix}-schema`,
      upgrade: `${prefix}-upgrade`,
      quota: `${prefix}-quota`,
      lease: `${prefix}-lease`,
      cleanup: `${prefix}-cleanup`,
      repair: `${prefix}-repair`,
      failedCreate: `${prefix}-failed-create`,
    };

    for (const name of Object.values(names)) await deleteDb(name);

    const storageState = {
      usage: 1024,
      quota: 100 * 1024 * 1024,
      persisted: false,
      persistResult: true,
      persistCalls: 0,
    };
    let fakeNow = Date.UTC(2026, 6, 25, 0, 0, 0);
    const schemaCache = YunOSPrivateCache.create({
      dbName: names.schema,
      storageManager: makeStorage(storageState),
      now: () => fakeNow,
      ownerId: "schema-owner",
    });
    await schemaCache.open();
    const schemaDb = await openRaw(names.schema, 1);
    const storeNames = Array.from(schemaDb.objectStoreNames);
    const schemaTransaction = schemaDb.transaction(storeNames, "readonly");
    const schemaShape = Object.fromEntries(storeNames.map((storeName) => {
      const store = schemaTransaction.objectStore(storeName);
      return [
        storeName,
        {
          keyPath: Array.isArray(store.keyPath) ? [...store.keyPath] : store.keyPath,
          indexes: Array.from(store.indexNames),
        },
      ];
    }));
    schemaDb.close();
    result.schema = {
      inspection: await schemaCache.inspect(),
      storeNames,
      schemaShape,
    };
    const expectedStores = [
      "conversations",
      "mediaObjects",
      "messages",
      "meta",
      "searchSnapshots",
      "segments",
      "thumbnails",
    ];
    if (JSON.stringify(storeNames.sort()) !== JSON.stringify(expectedStores.sort())) {
      throw new Error(`schema stores differ: ${storeNames.join(",")}`);
    }

    const fallbackStorage = {
      estimate: async () => { throw new Error("estimate unavailable"); },
      persisted: async () => { throw new Error("persisted unavailable"); },
      persist: async () => false,
    };
    const fallbackCache = YunOSPrivateCache.create({
      dbName: `${prefix}-fallback`,
      storageManager: fallbackStorage,
      ownerId: "fallback-owner",
    });
    names.fallback = `${prefix}-fallback`;
    await fallbackCache.open();
    const fallback = await fallbackCache.estimateCapacity();
    const estimated = await schemaCache.estimateCapacity();
    const persisted = await schemaCache.requestPersistence();
    result.capacity = {
      fallback,
      estimated,
      persisted,
      persistCalls: storageState.persistCalls,
    };
    if (fallback.effectiveTarget !== 256 * 1024 * 1024) {
      throw new Error("fallback target differs");
    }
    if (fallback.effectiveCeiling !== 512 * 1024 * 1024) {
      throw new Error("fallback ceiling differs");
    }
    if (estimated.effectiveTarget !== 50 * 1024 * 1024) {
      throw new Error("quota target differs");
    }
    if (estimated.effectiveCeiling !== 70 * 1024 * 1024) {
      throw new Error("quota ceiling differs");
    }
    if (storageState.persistCalls !== 1 || persisted.persisted !== true) {
      throw new Error("persistence request is not explicit and bounded");
    }

    const beforeTtl = await schemaCache.putRecord("conversations", {
      conversationId: "synthetic-ttl",
      title: "synthetic-only",
    });
    const validTtl = await schemaCache.getRecord("conversations", "synthetic-ttl", {
      touch: true,
    });
    fakeNow += YunOSPrivateCache.constants.ttlMs + 1;
    const hiddenExpired = await schemaCache.getRecord("conversations", "synthetic-ttl");
    const visibleExpired = await schemaCache.getRecord(
      "conversations",
      "synthetic-ttl",
      { allowExpired: true },
    );
    result.ttl = {
      validatedAt: beforeTtl.validatedAt,
      touchedValidatedAt: validTtl.validatedAt,
      hiddenExpired,
      visibleExpired: visibleExpired?.expired,
    };
    if (
      validTtl.validatedAt !== beforeTtl.validatedAt
      || hiddenExpired !== null
      || visibleExpired?.expired !== true
    ) {
      throw new Error("TTL validation semantics differ");
    }
    schemaCache.close();

    let failedCreate = false;
    const failedCreateCache = YunOSPrivateCache.create({
      dbName: names.failedCreate,
      migrationHook: () => { throw new Error("synthetic migration failure"); },
      ownerId: "failed-create-owner",
    });
    try {
      await failedCreateCache.open();
    } catch {
      failedCreate = true;
    }
    failedCreateCache.close();
    const afterFailedCreateNames = await databaseNames();
    if (!failedCreate || afterFailedCreateNames.includes(names.failedCreate)) {
      throw new Error("failed initial migration did not roll back");
    }

    const upgradeV1 = YunOSPrivateCache.create({
      dbName: names.upgrade,
      dbVersion: 1,
      storageManager: makeStorage(storageState),
      ownerId: "upgrade-v1",
    });
    await upgradeV1.open();
    await upgradeV1.putRecord("conversations", {
      conversationId: "synthetic-upgrade",
      title: "preserve-on-failure",
    });
    upgradeV1.close();
    let upgradeFailed = false;
    const upgradeV2Fail = YunOSPrivateCache.create({
      dbName: names.upgrade,
      dbVersion: 2,
      migrationHook: () => { throw new Error("synthetic v2 failure"); },
      ownerId: "upgrade-v2-fail",
    });
    try {
      await upgradeV2Fail.open();
    } catch {
      upgradeFailed = true;
    }
    upgradeV2Fail.close();
    const upgradeV1Readback = YunOSPrivateCache.create({
      dbName: names.upgrade,
      dbVersion: 1,
      storageManager: makeStorage(storageState),
      ownerId: "upgrade-v1-readback",
    });
    await upgradeV1Readback.open();
    const preserved = await upgradeV1Readback.getRecord(
      "conversations",
      "synthetic-upgrade",
    );
    upgradeV1Readback.close();
    if (!upgradeFailed || preserved?.title !== "preserve-on-failure") {
      throw new Error("failed upgrade did not preserve v1 data");
    }
    const upgradeV2 = YunOSPrivateCache.create({
      dbName: names.upgrade,
      dbVersion: 2,
      storageManager: makeStorage(storageState),
      ownerId: "upgrade-v2",
    });
    await upgradeV2.open();
    result.migration = {
      failedCreate,
      upgradeFailed,
      preserved: preserved?.title,
      final: (await upgradeV2.inspect()).migration,
    };
    upgradeV2.close();

    let writeAttempts = 0;
    const quotaCache = YunOSPrivateCache.create({
      dbName: names.quota,
      storageManager: makeStorage(storageState),
      ownerId: "quota-owner",
      writeAttemptHook: ({ attempt }) => {
        writeAttempts += 1;
        if (attempt === 0) throw new DOMException("synthetic quota", "QuotaExceededError");
      },
    });
    await quotaCache.open();
    await quotaCache.putRecord("conversations", {
      conversationId: "synthetic-quota",
      title: "retry-once",
    });
    const afterQuota = await quotaCache.inspect();
    const beforeCloneFailure = JSON.stringify(afterQuota.stats);
    let cloneFailed = false;
    try {
      await quotaCache.putRecord("conversations", {
        conversationId: "synthetic-clone-failure",
        invalid: () => {},
      });
    } catch {
      cloneFailed = true;
    }
    const afterCloneFailure = await quotaCache.inspect();
    result.quota = {
      writeAttempts,
      cloneFailed,
      statsStable: beforeCloneFailure === JSON.stringify(afterCloneFailure.stats),
      count: afterQuota.stats.categoryCounts.conversations,
    };
    if (
      writeAttempts !== 2
      || !cloneFailed
      || beforeCloneFailure !== JSON.stringify(afterCloneFailure.stats)
      || afterQuota.stats.categoryCounts.conversations !== 1
    ) {
      throw new Error("quota retry or atomic statistics differ");
    }
    quotaCache.close();

    const leaseA = YunOSPrivateCache.create({
      dbName: names.lease,
      storageManager: makeStorage(storageState),
      ownerId: "lease-a",
      now: () => fakeNow,
    });
    const leaseB = YunOSPrivateCache.create({
      dbName: names.lease,
      storageManager: makeStorage(storageState),
      ownerId: "lease-b",
      now: () => fakeNow,
    });
    await Promise.all([leaseA.open(), leaseB.open()]);
    const leaseDb = await openRaw(names.lease, 1);
    let leaseTx = leaseDb.transaction("meta", "readwrite");
    leaseTx.objectStore("meta").put({
      key: "writerLease",
      ownerId: "external-owner",
      leaseId: "external-lease",
      acquiredAt: fakeNow,
      renewedAt: fakeNow,
      expiresAt: fakeNow + 60_000,
    });
    await new Promise((resolve, reject) => {
      leaseTx.oncomplete = resolve;
      leaseTx.onerror = () => reject(leaseTx.error);
    });
    let leaseBlocked = false;
    try {
      await leaseB.putRecord("conversations", {
        conversationId: "synthetic-lease-blocked",
      });
    } catch (error) {
      leaseBlocked = error?.name === "LeaseBusyError";
    }
    leaseTx = leaseDb.transaction("meta", "readwrite");
    leaseTx.objectStore("meta").put({
      key: "writerLease",
      ownerId: "external-owner",
      leaseId: "expired-lease",
      acquiredAt: fakeNow - 120_000,
      renewedAt: fakeNow - 120_000,
      expiresAt: fakeNow - 1,
    });
    await new Promise((resolve, reject) => {
      leaseTx.oncomplete = resolve;
      leaseTx.onerror = () => reject(leaseTx.error);
    });
    await leaseB.putRecord("conversations", {
      conversationId: "synthetic-lease-recovered",
    });
    leaseDb.close();
    result.lease = {
      blocked: leaseBlocked,
      recovered: Boolean(await leaseA.getRecord(
        "conversations",
        "synthetic-lease-recovered",
      )),
    };
    if (!result.lease.blocked || !result.lease.recovered) {
      throw new Error("single writer lease gate differs");
    }
    leaseA.close();
    leaseB.close();

    const cleanupStorage = {
      usage: 0,
      quota: 1024 * 1024 * 1024,
      persisted: false,
      persistResult: false,
      persistCalls: 0,
    };
    let abortOnYield = false;
    let cleanupController = new AbortController();
    const cleanupCache = YunOSPrivateCache.create({
      dbName: names.cleanup,
      storageManager: makeStorage(cleanupStorage),
      ownerId: "cleanup-owner",
      yieldControl: async () => {
        if (abortOnYield) cleanupController.abort();
        await Promise.resolve();
      },
    });
    await cleanupCache.open();
    for (let index = 0; index < 24; index += 1) {
      await cleanupCache.putRecord("searchSnapshots", {
        snapshotKey: `synthetic-snapshot-${String(index).padStart(2, "0")}`,
        query: "synthetic",
        payload: "x".repeat(500),
        resultRefs: [],
      });
    }
    const beforeCleanup = await cleanupCache.inspect();
    cleanupStorage.quota = 6000;
    abortOnYield = true;
    const interrupted = await cleanupCache.cleanup({
      signal: cleanupController.signal,
      batchSize: 2,
    });
    abortOnYield = false;
    cleanupController = new AbortController();
    const resumed = await cleanupCache.cleanup({
      signal: cleanupController.signal,
      batchSize: 2,
    });
    const afterCleanup = await cleanupCache.inspect();
    result.cleanup = {
      beforeBytes: beforeCleanup.stats.totalBytes,
      interrupted: interrupted.interrupted,
      interruptedState: interrupted.interrupted
        ? "interrupted"
        : "unexpected",
      resumedBytes: resumed.totalBytes,
      finalState: afterCleanup.cleanup.state,
      finalCount: afterCleanup.stats.categoryCounts.searchSnapshots,
    };
    if (
      interrupted.interrupted !== true
      || afterCleanup.cleanup.state !== "idle"
      || afterCleanup.stats.totalBytes > Math.floor(6000 * 0.5 * 0.9)
      || afterCleanup.stats.categoryCounts.searchSnapshots >= 24
    ) {
      throw new Error("cleanup interruption/resume or target differs");
    }
    cleanupCache.close();

    const repairCache = YunOSPrivateCache.create({
      dbName: names.repair,
      storageManager: makeStorage(storageState),
      ownerId: "repair-owner",
    });
    await repairCache.open();
    await repairCache.putRecord("messages", {
      conversationId: "synthetic-conversation",
      messageId: "synthetic-message",
      role: "user",
      text: "synthetic-only",
    });
    await repairCache.putRecord("segments", {
      segmentKey: "synthetic-segment",
      conversationId: "synthetic-conversation",
      messageIds: ["synthetic-message", "missing-message"],
    });
    await repairCache.putRecord("searchSnapshots", {
      snapshotKey: "synthetic-repair-snapshot",
      resultRefs: [
        { conversationId: "synthetic-conversation", messageId: "synthetic-message" },
        { conversationId: "synthetic-conversation", messageId: "missing-message" },
      ],
    });
    await repairCache.putRecord("mediaObjects", {
      mediaKey: "synthetic-broken-media",
      kind: "attachment",
      complete: true,
      contentLength: 128,
    });
    const repairResult = await repairCache.repair({ batchSize: 2 });
    const repairedSegment = await repairCache.getRecord("segments", "synthetic-segment");
    const repairedSnapshot = await repairCache.getRecord(
      "searchSnapshots",
      "synthetic-repair-snapshot",
    );
    const removedMedia = await repairCache.getRecord(
      "mediaObjects",
      "synthetic-broken-media",
    );
    result.repair = {
      repairResult,
      segmentPartial: repairedSegment?.partial,
      missingMessageIds: repairedSegment?.missingMessageIds,
      snapshotRefs: repairedSnapshot?.resultRefs?.length,
      mediaRemoved: removedMedia === null,
    };
    if (
      repairedSegment?.partial !== true
      || repairedSegment?.missingMessageIds?.length !== 1
      || repairedSnapshot?.resultRefs?.length !== 1
      || removedMedia !== null
    ) {
      throw new Error("repair framework result differs");
    }

    const resumeDb = await openRaw("yunos-mobile-resume", 1, (database) => {
      database.createObjectStore("resumeStates", { keyPath: "key" });
    });
    resumeDb.close();
    localStorage.setItem("yunos.mobileSearch.history.v1", '{"synthetic":true}');
    const shellBeforeClear = await caches.keys();
    const clearResult = await repairCache.clearPrivateCache();
    const afterClear = await repairCache.inspect();
    const shellAfterClear = await caches.keys();
    const afterClearNames = await databaseNames();
    result.clear = {
      clearResult,
      totalBytes: afterClear.stats.totalBytes,
      categoryCounts: afterClear.stats.categoryCounts,
      resumeExists: afterClearNames.includes("yunos-mobile-resume"),
      legacyState: localStorage.getItem("yunos.mobileSearch.history.v1"),
      shellNamesUnchanged: JSON.stringify(shellBeforeClear) === JSON.stringify(shellAfterClear),
    };
    if (
      afterClear.stats.totalBytes !== 0
      || Object.values(afterClear.stats.categoryCounts).some((value) => value !== 0)
      || result.clear.resumeExists
      || result.clear.legacyState !== null
      || !result.clear.shellNamesUnchanged
      || clearResult.appShellPreserved !== true
    ) {
      throw new Error("safe clear result differs");
    }
    repairCache.close();

    for (const name of Object.values(names)) {
      await deleteDb(name);
    }
    return { result, testDatabases: Object.values(names) };
  });
  testDatabases.push(...core.testDatabases);
  assert(pageErrors.length === 0, `online page errors: ${pageErrors.join(" | ")}`);
  assert(consoleErrors.length === 0, `online console errors: ${consoleErrors.join(" | ")}`);

  await page.context().setOffline(true);
  await page.reload({ waitUntil: "domcontentloaded" });
  await waitUntil(
    () => page.evaluate(() => document.body.dataset.yunosPrivateCacheState === "ready"),
    "private cache core did not initialize from the offline application shell",
  );
  const offline = await page.evaluate(async () => ({
    offlineShell: document.body.dataset.yunosOfflineShell === "true",
    mobileShellPresent: Boolean(document.querySelector("#yunos-mobile-shell")),
    privateCacheState: document.body.dataset.yunosPrivateCacheState,
    cacheNames: await caches.keys(),
  }));
  await page.context().setOffline(false);
  assert(!offline.offlineShell, "navigation returned the minimal offline error shell");
  assert(offline.mobileShellPresent, "offline navigation did not start the full application shell");
  assert(offline.privateCacheState === "ready", "private cache is unavailable offline");
  assert(
    apiRequests.every((request) => request.method === "GET"),
    "mobile browser emitted a non-GET API request",
  );
  assert(pageErrors.length === 0, `page errors: ${pageErrors.join(" | ")}`);
  const unexpectedConsoleErrors = consoleErrors.filter(
    (message) => message !== "Failed to load resource: net::ERR_FAILED",
  );
  assert(
    unexpectedConsoleErrors.length === 0,
    `console errors: ${unexpectedConsoleErrors.join(" | ")}`,
  );

  const desktopConsoleStart = consoleErrors.length;
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.reload({ waitUntil: "networkidle" });
  const desktopShell = page.locator("#yunos-desktop-shell");
  const mobileShell = page.locator("#yunos-mobile-shell");
  assert(await desktopShell.isVisible(), "v8.1 desktop shell is not visible");
  assert(!await mobileShell.isVisible(), "mobile shell leaked into the desktop viewport");
  await page.locator("#shell-sidebar-toggle").click();
  await page.locator('#yunos-desktop-shell[data-sidebar-open="true"]').waitFor();
  await page.locator("#shell-sidebar-toggle").click();
  await page.locator('#yunos-desktop-shell[data-sidebar-open="false"]').waitFor();
  const desktop = {
    shellVisible: await desktopShell.isVisible(),
    mobileShellVisible: await mobileShell.isVisible(),
    omniboxVisible: await page.locator("#home-omnibox-input").isVisible(),
    sidebarOpen: await desktopShell.getAttribute("data-sidebar-open"),
  };
  assert(desktop.omniboxVisible, "v8.1 desktop omnibox is not visible");
  assert(desktop.sidebarOpen === "false", "v8.1 desktop sidebar did not close");
  const desktopConsoleErrors = consoleErrors.slice(desktopConsoleStart);
  assert(
    desktopConsoleErrors.length === 0,
    `desktop console errors: ${desktopConsoleErrors.join(" | ")}`,
  );
  assert(
    apiRequests.every((request) => request.method === "GET"),
    "desktop regression emitted a non-GET API request",
  );

  return {
    status: "passed",
    apiRequestCount: apiRequests.length,
    apiMethods: [...new Set(apiRequests.map((request) => request.method))],
    consoleErrors: unexpectedConsoleErrors,
    expectedOfflineNetworkErrors: consoleErrors.length - unexpectedConsoleErrors.length,
    pageErrors,
    initial,
    core: core.result,
    offline,
    desktop,
    testDatabases,
  };
}
