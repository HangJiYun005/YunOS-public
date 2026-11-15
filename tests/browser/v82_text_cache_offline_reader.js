async (page, harness = {}) => {
  const baseUrl = harness.baseUrl || "http://127.0.0.1:8782/";
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
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await waitUntil(
    () => page.evaluate(() => document.body.dataset.yunosPrivateCacheState === "ready"),
    "private cache core did not initialize",
  );

  return page.evaluate(async () => {
    const assert = (condition, message) => {
      if (!condition) throw new Error(message);
    };
    const deleteDb = (name) => new Promise((resolve, reject) => {
      const request = indexedDB.deleteDatabase(name);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error(`delete blocked: ${name}`));
    });
    const openRaw = (name) => new Promise((resolve, reject) => {
      const request = indexedDB.open(name);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const transactionDone = (transaction) => new Promise((resolve, reject) => {
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
    const prefix = `yunos-e2-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const names = {
      main: `${prefix}-main`,
      atomic: `${prefix}-atomic`,
      quota: `${prefix}-quota`,
      multi: `${prefix}-multi`,
      ttl: `${prefix}-ttl`,
      protected: `${prefix}-protected`,
    };
    for (const name of Object.values(names)) await deleteDb(name);

    const storageState = {
      quota: 1024 * 1024 * 1024,
      persisted: false,
    };
    const storageManager = {
      estimate: async () => ({ usage: 0, quota: storageState.quota }),
      persisted: async () => storageState.persisted,
      persist: async () => false,
    };
    let fakeNow = Date.UTC(2026, 6, 25, 8, 0, 0);
    const messages = (start, end, prefixText = "message") => (
      Array.from({ length: end - start + 1 }, (_, offset) => {
        const position = start + offset;
        return {
          conversationId: "conversation-main",
          messageId: `message-${position}`,
          role: position % 2 ? "user" : "assistant",
          text: `${prefixText}-${position}`,
          messageTime: new Date(fakeNow + position * 1000).toISOString(),
          position,
        };
      })
    );

    const main = YunOSPrivateCache.create({
      dbName: names.main,
      storageManager,
      now: () => fakeNow,
      ownerId: "main-owner",
    });
    await main.open();
    const preview = await main.saveMessageWindow({
      conversationId: "conversation-main",
      conversationTitle: "Main conversation",
      anchorMessageId: "message-10",
      direction: "preview",
      expectedMessageCount: 5,
      reachedStart: "unknown",
      reachedEnd: "unknown",
      messages: messages(8, 12),
    });
    const previewRead = await main.getCachedMessageContext({
      conversationId: "conversation-main",
      anchorMessageId: "message-10",
      expectedMessageCount: 5,
    });
    assert(previewRead.status === "ready", "five-message preview was not readable");
    assert(previewRead.messages.length === 5, "preview message count differs");

    await main.saveMessageWindow({
      conversationId: "conversation-main",
      conversationTitle: "Main conversation",
      anchorMessageId: "message-10",
      direction: "initial",
      cursorAfter: "cursor-after-20",
      reachedStart: true,
      reachedEnd: false,
      messages: messages(1, 20),
    });
    const beforeUpdate = await main.getRecord(
      "messages",
      ["conversation-main", "message-20"],
    );
    fakeNow += 1000;
    const overlap = await main.saveMessageWindow({
      conversationId: "conversation-main",
      conversationTitle: "Main conversation",
      anchorMessageId: "message-20",
      direction: "after",
      requestCursor: "cursor-after-20",
      reachedStart: false,
      reachedEnd: true,
      messages: messages(15, 25, "updated-message-with-more-text"),
    });
    const afterUpdate = await main.getRecord(
      "messages",
      ["conversation-main", "message-20"],
    );
    assert(afterUpdate.text.startsWith("updated-message"), "message update was not stored");
    assert(afterUpdate.byteSize > beforeUpdate.byteSize, "message byteSize was not recalculated");
    const mainInspection = await main.inspect();
    assert(mainInspection.stats.categoryCounts.messages === 25, "overlap was not deduplicated");
    assert(mainInspection.stats.categoryCounts.segments === 3, "segment count differs");
    assert(mainInspection.stats.categoryCounts.searchSnapshots === 0, "search snapshot was written");
    assert(mainInspection.stats.categoryCounts.thumbnails === 0, "thumbnail was written");
    assert(mainInspection.stats.categoryCounts.mediaObjects === 0, "media was written");

    const reader = await main.getCachedConversationWindow({
      conversationId: "conversation-main",
      anchorMessageId: "message-10",
    });
    assert(reader.status === "ready", "cached reader was not readable");
    assert(reader.messages.length === 25, "cached reader dedupe differs");
    assert(reader.reachedStart === true, "reader start boundary differs");
    assert(reader.reachedEnd === true, "reader end boundary differs");
    assert(reader.gapsBeforeMessageIds.length === 0, "overlapping segments created a false gap");
    const catalog = await main.getCachedConversationCatalog();
    assert(catalog.length === 1, "cached catalog differs");
    assert(catalog[0].messageCount === 25, "catalog message estimate differs");

    const validatedBeforeTouch = afterUpdate.validatedAt;
    const accessedBeforeTouch = afterUpdate.lastAccessedAt;
    fakeNow += 1000;
    await main.getCachedConversationWindow({
      conversationId: "conversation-main",
      anchorMessageId: "message-20",
    });
    const afterTouch = await main.getRecord(
      "messages",
      ["conversation-main", "message-20"],
    );
    assert(afterTouch.validatedAt === validatedBeforeTouch, "local read extended validation");
    assert(afterTouch.lastAccessedAt > accessedBeforeTouch, "local read did not touch LRU time");

    await main.saveMessageWindow({
      conversationId: "conversation-gap",
      conversationTitle: "Gap conversation",
      anchorMessageId: "gap-3",
      direction: "initial",
      reachedStart: true,
      reachedEnd: false,
      messages: Array.from({ length: 5 }, (_, index) => ({
        conversationId: "conversation-gap",
        messageId: `gap-${index + 1}`,
        role: "user",
        text: `gap-${index + 1}`,
        position: index + 1,
      })),
    });
    await main.saveMessageWindow({
      conversationId: "conversation-gap",
      conversationTitle: "Gap conversation",
      anchorMessageId: "gap-13",
      direction: "after",
      requestCursor: "gap-cursor",
      reachedStart: false,
      reachedEnd: true,
      messages: Array.from({ length: 5 }, (_, index) => ({
        conversationId: "conversation-gap",
        messageId: `gap-${index + 11}`,
        role: "assistant",
        text: `gap-${index + 11}`,
        position: index + 11,
      })),
    });
    const gapReader = await main.getCachedConversationWindow({
      conversationId: "conversation-gap",
      anchorMessageId: "gap-3",
    });
    assert(
      gapReader.gapsBeforeMessageIds.includes("gap-11"),
      "middle cache gap was not reported",
    );

    const raw = await openRaw(names.main);
    const corruptTransaction = raw.transaction("messages", "readwrite");
    corruptTransaction.objectStore("messages").delete(
      ["conversation-main", "message-25"],
    );
    await transactionDone(corruptTransaction);
    raw.close();
    const corruptRead = await main.getCachedConversationWindow({
      conversationId: "conversation-main",
      anchorMessageId: "message-25",
    });
    assert(corruptRead.status === "incomplete", "corrupt segment was displayed");
    await main.repair({ batchSize: 10 });
    const repairedSegment = await main.getRecord("segments", overlap.segment.segmentKey);
    assert(repairedSegment.partial === true, "invalid segment reference was not marked partial");

    const atomic = YunOSPrivateCache.create({
      dbName: names.atomic,
      storageManager,
      ownerId: "atomic-owner",
      messageWindowMutationHook: () => {
        throw new Error("synthetic mid-transaction failure");
      },
    });
    await atomic.open();
    let atomicFailed = false;
    try {
      await atomic.saveMessageWindow({
        conversationId: "atomic-conversation",
        conversationTitle: "Atomic",
        anchorMessageId: "atomic-1",
        direction: "initial",
        messages: [{
          conversationId: "atomic-conversation",
          messageId: "atomic-1",
          role: "user",
          text: "must roll back",
          position: 1,
        }],
      });
    } catch {
      atomicFailed = true;
    }
    const atomicInspection = await atomic.inspect();
    assert(atomicFailed, "synthetic transaction did not fail");
    assert(atomicInspection.stats.totalBytes === 0, "failed transaction drifted bytes");
    assert(
      atomicInspection.stats.categoryCounts.messages === 0
      && atomicInspection.stats.categoryCounts.segments === 0
      && atomicInspection.stats.categoryCounts.conversations === 0,
      "failed transaction left partial records",
    );

    let quotaAttempts = 0;
    const quota = YunOSPrivateCache.create({
      dbName: names.quota,
      storageManager,
      ownerId: "quota-owner",
      writeAttemptHook: ({ category, attempt }) => {
        if (category !== "messageWindow") return;
        quotaAttempts += 1;
        if (attempt === 0) {
          throw new DOMException("synthetic quota", "QuotaExceededError");
        }
      },
    });
    await quota.open();
    await quota.saveMessageWindow({
      conversationId: "quota-conversation",
      conversationTitle: "Quota",
      anchorMessageId: "quota-1",
      direction: "initial",
      messages: [{
        conversationId: "quota-conversation",
        messageId: "quota-1",
        role: "user",
        text: "retry once",
        position: 1,
      }],
    });
    assert(quotaAttempts === 2, "quota retry was not exactly once");

    const multiA = YunOSPrivateCache.create({
      dbName: names.multi,
      storageManager,
      ownerId: "multi-a",
    });
    const multiB = YunOSPrivateCache.create({
      dbName: names.multi,
      storageManager,
      ownerId: "multi-b",
    });
    await Promise.all([multiA.open(), multiB.open()]);
    const multiWindow = {
      conversationId: "multi-conversation",
      conversationTitle: "Multi",
      anchorMessageId: "multi-1",
      direction: "initial",
      messages: [{
        conversationId: "multi-conversation",
        messageId: "multi-1",
        role: "user",
        text: "same record",
        position: 1,
      }],
    };
    await multiA.saveMessageWindow(multiWindow);
    await multiB.saveMessageWindow(multiWindow);
    const multiInspection = await multiA.inspect();
    assert(multiInspection.stats.categoryCounts.messages === 1, "multi-page message count drifted");
    assert(multiInspection.stats.categoryCounts.segments === 1, "multi-page segment count drifted");

    let ttlNow = Date.UTC(2026, 6, 25, 0, 0, 0);
    const ttl = YunOSPrivateCache.create({
      dbName: names.ttl,
      storageManager,
      now: () => ttlNow,
      ownerId: "ttl-owner",
    });
    await ttl.open();
    await ttl.saveMessageWindow({
      conversationId: "ttl-conversation",
      conversationTitle: "TTL",
      anchorMessageId: "ttl-3",
      direction: "preview",
      expectedMessageCount: 5,
      messages: Array.from({ length: 5 }, (_, index) => ({
        conversationId: "ttl-conversation",
        messageId: `ttl-${index + 1}`,
        role: "user",
        text: `ttl-${index + 1}`,
        position: index + 1,
      })),
    });
    ttlNow += YunOSPrivateCache.constants.ttlMs + 1;
    const expired = await ttl.getCachedMessageContext({
      conversationId: "ttl-conversation",
      anchorMessageId: "ttl-3",
      expectedMessageCount: 5,
    });
    assert(expired.status === "expired", "30-day TTL did not expire the preview");

    const protectedStorage = {
      quota: 1024 * 1024 * 1024,
    };
    const protectedCache = YunOSPrivateCache.create({
      dbName: names.protected,
      storageManager: {
        estimate: async () => ({ usage: 0, quota: protectedStorage.quota }),
        persisted: async () => false,
        persist: async () => false,
      },
      ownerId: "protected-owner",
    });
    await protectedCache.open();
    const protectedWindow = await protectedCache.saveMessageWindow({
      conversationId: "protected-conversation",
      conversationTitle: "Protected",
      anchorMessageId: "protected-2",
      direction: "initial",
      reachedStart: true,
      reachedEnd: true,
      messages: Array.from({ length: 3 }, (_, index) => ({
        conversationId: "protected-conversation",
        messageId: `protected-${index + 1}`,
        role: "assistant",
        text: "p".repeat(600),
        position: index + 1,
      })),
    });
    await protectedCache.markActiveReaderWindow({
      conversationId: "protected-conversation",
      segmentKeys: [protectedWindow.segment.segmentKey],
      messageIds: protectedWindow.segment.messageIds,
    });
    for (let index = 0; index < 8; index += 1) {
      await protectedCache.putRecord("searchSnapshots", {
        snapshotKey: `discard-${index}`,
        payload: "x".repeat(700),
        resultRefs: [],
      });
    }
    protectedStorage.quota = 6000;
    await protectedCache.cleanup({ batchSize: 2 });
    const protectedRead = await protectedCache.getCachedConversationWindow({
      conversationId: "protected-conversation",
      anchorMessageId: "protected-2",
    });
    const protectedInspection = await protectedCache.inspect();
    assert(protectedRead.status === "ready", "active reader window was evicted");
    assert(
      protectedInspection.stats.categoryCounts.searchSnapshots < 8,
      "lower-priority snapshots were not cleaned",
    );
    await protectedCache.releaseActiveReaderWindow();

    const shellBeforeClear = await caches.keys();
    const clearResult = await main.clearPrivateCache({
      clearResume: false,
      clearLegacyState: false,
    });
    const afterClear = await main.inspect();
    const shellAfterClear = await caches.keys();
    assert(afterClear.stats.totalBytes === 0, "safe clear left private bytes");
    assert(
      Object.values(afterClear.stats.categoryCounts).every((count) => count === 0),
      "safe clear left private records",
    );
    assert(
      JSON.stringify(shellBeforeClear) === JSON.stringify(shellAfterClear),
      "safe clear changed application shell caches",
    );
    assert(clearResult.appShellPreserved === true, "safe clear did not report shell preservation");

    for (const cache of [main, atomic, quota, multiA, multiB, ttl, protectedCache]) {
      cache.close();
    }
    for (const name of Object.values(names)) await deleteDb(name);

    return {
      previewMessages: previewRead.messages.length,
      uniqueMessages: mainInspection.stats.categoryCounts.messages,
      segments: mainInspection.stats.categoryCounts.segments,
      gapBefore: gapReader.gapsBeforeMessageIds,
      atomicRollback: atomicFailed,
      quotaAttempts,
      ttlStatus: expired.status,
      protectedStatus: protectedRead.status,
      protectedSnapshotsRemaining:
        protectedInspection.stats.categoryCounts.searchSnapshots,
      searchSnapshots: mainInspection.stats.categoryCounts.searchSnapshots,
      thumbnails: mainInspection.stats.categoryCounts.thumbnails,
      mediaObjects: mainInspection.stats.categoryCounts.mediaObjects,
    };
  });
}
