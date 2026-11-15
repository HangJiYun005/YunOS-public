async (page, harness) => {
  const conversationId = "6a51d386-3790-83ea-b76c-c41c4e43914c";
  const knownEndMessageId = "bf460cc9-4977-45ff-8007-68a21ed69cb7";
  const apiRequests = [];
  const pageErrors = [];
  const consoleErrors = [];

  page.on("request", (request) => {
    if (request.url().includes("/api/")) {
      apiRequests.push({
        method: request.method(),
        url: request.url(),
      });
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const readerState = () => page.evaluate(() => ({
    count: document.querySelectorAll(
      "#mobile-reader-messages .yunos-mobile-reader-message",
    ).length,
    ids: [...document.querySelectorAll(
      "#mobile-reader-messages .yunos-mobile-reader-message",
    )].map((node) => node.dataset.messageId),
    offline: !document.querySelector("#mobile-reader-offline-banner")?.hidden,
    cacheTime: document.querySelector("#mobile-reader-cache-time")?.textContent?.trim() || "",
    topStatus: document.querySelector("#mobile-reader-top-status")?.textContent?.trim() || "",
    bottomStatus:
      document.querySelector("#mobile-reader-bottom-status")?.textContent?.trim() || "",
    errors: [
      ...document.querySelectorAll(".yunos-mobile-reader-boundary.is-error"),
    ].map((node) => node.textContent?.trim() || ""),
    gaps: document.querySelectorAll(".yunos-mobile-reader-gap").length,
    horizontalOverflow: Math.max(
      document.documentElement.scrollWidth - document.documentElement.clientWidth,
      document.body.scrollWidth - document.body.clientWidth,
    ),
  }));
  const visibleAnchor = () => page.evaluate(() => {
    const scroll = document.querySelector("#mobile-reader-scroll");
    const bounds = scroll.getBoundingClientRect();
    const node = [...document.querySelectorAll(
      "#mobile-reader-messages .yunos-mobile-reader-message",
    )].find((candidate) => candidate.getBoundingClientRect().bottom > bounds.top + 1);
    return node
      ? {
        messageId: node.dataset.messageId || "",
        offset: node.getBoundingClientRect().top - bounds.top,
      }
      : null;
  });
  const waitForCache = async (predicate, message) => {
    await page.waitForFunction(
      async (predicateSource) => {
        const result = await globalThis.yunosPrivateCache.inspect();
        return Function("value", `return (${predicateSource})(value);`)(result);
      },
      predicate.toString(),
      { timeout: 12_000 },
    ).catch(() => {
      throw new Error(message);
    });
  };
  const waitForReaderCount = async (minimum) => {
    await page.waitForFunction(
      (expected) => document.querySelectorAll(
        "#mobile-reader-messages .yunos-mobile-reader-message",
      ).length >= expected,
      minimum,
      { timeout: 12_000 },
    );
  };

  await page.goto(harness.baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.body.dataset.yunosPrivateCacheState === "ready",
  );
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
    await globalThis.yunosPrivateCache.clearPrivateCache({
      clearResume: false,
      clearLegacyState: false,
    });
  });
  if (!await page.evaluate(() => Boolean(navigator.serviceWorker.controller))) {
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.body.dataset.yunosPrivateCacheState === "ready",
    );
  }
  assert(
    await page.evaluate(() => Boolean(navigator.serviceWorker.controller)),
    "the E2 application shell is not controlled by the service worker",
  );

  const anchors = await page.evaluate(async ({ conversationId, knownEndMessageId }) => {
    const first = await fetch(
      `/api/conversation-messages?conversation_id=${encodeURIComponent(conversationId)}`
      + `&anchor_message_id=${encodeURIComponent(knownEndMessageId)}&limit=20`,
    ).then((response) => response.json());
    const second = await fetch(
      `/api/conversation-messages?conversation_id=${encodeURIComponent(conversationId)}`
      + `&before=${encodeURIComponent(first.cursors.before)}&limit=20`,
    ).then((response) => response.json());
    const anchor = second.messages[Math.floor(second.messages.length / 2)];
    return {
      messageId: anchor.message_id,
      title: second.conversation.title,
      position: anchor.position,
      firstCount: first.messages.length,
      secondCount: second.messages.length,
    };
  }, { conversationId, knownEndMessageId });
  assert(anchors.firstCount === 20 && anchors.secondCount === 20, "real API did not return 20-row windows");

  await page.evaluate(async ({ conversationId, messageId, title }) => {
    await loadMobileContextPreview(
      { conversation_id: conversationId, title },
      { conversation_id: conversationId, message_id: messageId },
      "e2-real-preview",
    );
  }, { conversationId, messageId: anchors.messageId, title: anchors.title });
  await waitForCache(
    (value) => value.stats?.categoryCounts?.segments >= 1
      && value.stats?.categoryCounts?.messages >= 5,
    "five-message preview was not persisted",
  );
  const preview = await page.evaluate(async ({ conversationId, messageId }) => {
    const cached = await globalThis.yunosPrivateCache.getCachedMessageContext({
      conversationId,
      anchorMessageId: messageId,
      expectedMessageCount: 5,
      touch: false,
    });
    return {
      status: cached.status,
      count: cached.messages?.length || 0,
      anchor: cached.anchorMessageId,
    };
  }, { conversationId, messageId: anchors.messageId });
  assert(preview.status === "ready" && preview.count === 5, "preview cache is not an exact five-message window");

  const readerUrl = `${harness.baseUrl}?mobile_conversation=${encodeURIComponent(conversationId)}`
    + `&focus_message=${encodeURIComponent(anchors.messageId)}`;
  await page.goto(readerUrl, { waitUntil: "domcontentloaded" });
  await waitForReaderCount(20);
  await page.waitForFunction(() => mobileConversationState.positioned === true);
  const initial = await readerState();
  assert(initial.count === 20, `initial reader expected 20 messages, received ${initial.count}`);
  assert(!initial.offline, "online reader incorrectly shows the offline banner");

  const beforeAnchor = await visibleAnchor();
  await page.evaluate(() => loadMobileConversationSegment("before"));
  await waitForReaderCount(40);
  const afterPrependAnchor = await visibleAnchor();
  assert(
    beforeAnchor?.messageId === afterPrependAnchor?.messageId
      && Math.abs(beforeAnchor.offset - afterPrependAnchor.offset) <= 2,
    `prepend drifted the visible anchor: ${JSON.stringify({ beforeAnchor, afterPrependAnchor })}`,
  );

  await page.evaluate(() => loadMobileConversationSegment("after"));
  await waitForReaderCount(60);
  await waitForCache(
    (value) => value.stats?.categoryCounts?.segments >= 4
      && value.stats?.categoryCounts?.messages >= 60,
    "reader windows were not persisted after paging",
  );
  const online = await readerState();
  assert(new Set(online.ids).size === online.ids.length, "reader contains duplicate message ids");
  assert(online.horizontalOverflow <= 1, `mobile reader overflows horizontally by ${online.horizontalOverflow}px`);

  const selection = await page.evaluate(() => {
    const text = document.querySelector("#mobile-reader-messages .yunos-mobile-reader-text");
    const node = text?.firstChild;
    if (!node || !node.textContent) return "";
    const range = document.createRange();
    range.setStart(node, 0);
    range.setEnd(node, Math.min(8, node.textContent.length));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return selection.toString();
  });
  assert(selection.length > 0, "reader message text could not be selected");

  const onlineInspect = await page.evaluate(async () => {
    const inspect = await globalThis.yunosPrivateCache.inspect();
    const cacheNames = await caches.keys();
    const cacheUrls = [];
    for (const name of cacheNames) {
      const cache = await caches.open(name);
      cacheUrls.push(...(await cache.keys()).map((request) => request.url));
    }
    const cached = await globalThis.yunosPrivateCache.getCachedConversationWindow({
      conversationId: mobileConversationState.conversationId,
      anchorMessageId: mobileConversationState.focusMessageId,
      touch: false,
    });
    const raw = await new Promise((resolve, reject) => {
      const request = indexedDB.open(globalThis.yunosPrivateCache.constants.dbName);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const database = request.result;
        const transaction = database.transaction(["messages", "segments"], "readonly");
        const messageRequest = transaction.objectStore("messages").count();
        const segmentRequest = transaction.objectStore("segments").getAll();
        transaction.oncomplete = () => resolve({
          messages: messageRequest.result,
          segments: segmentRequest.result.map((segment) => ({
            direction: segment.direction,
            messageCount: segment.messageIds?.length || 0,
          })),
        });
        transaction.onerror = () => reject(transaction.error);
      };
    });
    return {
      inspect,
      raw,
      cacheNames,
      cacheUrls,
      validatedAt: cached.validatedAt,
      cachedCount: cached.messages.length,
      gaps: cached.gapsBeforeMessageIds.length,
      reachedStart: cached.reachedStart,
      reachedEnd: cached.reachedEnd,
    };
  });
  assert(
    onlineInspect.raw.messages >= 60 && onlineInspect.raw.segments.length >= 4,
    `paged records were not actually persisted: ${JSON.stringify({
      raw: onlineInspect.raw,
      stats: onlineInspect.inspect.stats,
      consoleErrors,
    })}`,
  );
  assert(
    onlineInspect.cacheUrls.every((url) => !new URL(url).pathname.startsWith("/api/")),
    "an API response was found in CacheStorage",
  );
  assert(
    onlineInspect.inspect.stats.categoryCounts.searchSnapshots === 0,
    "search results were cached",
  );
  assert(onlineInspect.inspect.stats.categoryCounts.thumbnails === 0, "thumbnails were cached");
  assert(onlineInspect.inspect.stats.categoryCounts.mediaObjects === 0, "media objects were cached");
  await page.screenshot({
    path: `${harness.artifactDir}/online-reader.png`,
    fullPage: true,
  });

  const recoveryAnchorBefore = await visibleAnchor();
  await harness.stopServer();
  await page.context().setOffline(true);
  await page.goto(readerUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => document.body.dataset.yunosPrivateCacheState === "ready",
  );
  await page.waitForFunction(
    () => mobileConversationState.loadingInitial === false,
    null,
    { timeout: 20_000 },
  );
  const offlineLoaded = await readerState();
  if (offlineLoaded.count < 60) {
    const diagnostic = await page.evaluate(async () => ({
      reader: {
        conversationId: mobileConversationState.conversationId,
        focusMessageId: mobileConversationState.focusMessageId,
        initialError: mobileConversationState.initialError,
        offline: mobileConversationState.offline,
        count: mobileConversationState.messages.length,
      },
      catalog: await globalThis.yunosPrivateCache.getCachedConversationCatalog(),
      cache: await globalThis.yunosPrivateCache.getCachedConversationWindow({
        conversationId: mobileConversationState.conversationId,
        anchorMessageId: mobileConversationState.focusMessageId,
        touch: false,
      }),
      rawCounts: await new Promise((resolve, reject) => {
        const request = indexedDB.open(globalThis.yunosPrivateCache.constants.dbName);
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const database = request.result;
          const transaction = database.transaction(["messages", "segments"], "readonly");
          const messageRequest = transaction.objectStore("messages").count();
          const segmentRequest = transaction.objectStore("segments").getAll();
          transaction.oncomplete = () => resolve({
            messages: messageRequest.result,
            segments: segmentRequest.result.map((segment) => ({
              direction: segment.direction,
              messageCount: segment.messageIds?.length || 0,
              partial: segment.partial,
              missing: segment.missingMessageIds?.length || 0,
            })),
          });
          transaction.onerror = () => reject(transaction.error);
        };
      }),
    }));
    throw new Error(
      `offline reader expected 60 cached messages: ${JSON.stringify({
        ...diagnostic,
        consoleErrors,
      })}`,
    );
  }
  await page.waitForFunction(
    () => !document.querySelector("#mobile-reader-offline-banner")?.hidden,
  );
  const offline = await readerState();
  assert(offline.offline && offline.cacheTime, "offline reader lacks its banner or cache time");
  assert(
    !offline.topStatus.includes("已到达") && offline.bottomStatus.includes("已到达"),
    `offline reader did not preserve explicit server boundaries: ${JSON.stringify({
      topStatus: offline.topStatus,
      bottomStatus: offline.bottomStatus,
    })}`,
  );
  assert(offline.errors.length === 0, "offline cached reader rendered a network error");
  assert(offline.horizontalOverflow <= 1, "offline reader has horizontal overflow");
  await page.screenshot({
    path: `${harness.artifactDir}/offline-reader.png`,
    fullPage: true,
  });

  await page.evaluate(
    () => globalThis.yunosPrivateCache.discardResumeState("catalog-test"),
  );
  await page.goto(harness.baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(
    () => !document.querySelector("#mobile-offline-home")?.hidden,
  );
  await page.waitForFunction(
    () => document.querySelectorAll("#mobile-offline-conversations [role='listitem']").length >= 1,
  );
  const offlineHome = await page.evaluate(() => ({
    inputDisabled: document.querySelector("#mobile-search-input")?.disabled,
    placeholder: document.querySelector("#mobile-search-input")?.placeholder || "",
    catalogCount: document.querySelectorAll(
      "#mobile-offline-conversations [role='listitem']",
    ).length,
  }));
  assert(!offlineHome.inputDisabled, "offline home disabled E3 cached search");
  assert(
    offlineHome.placeholder.includes("搜索已缓存内容"),
    "offline home does not label cached-only search",
  );
  assert(
    offlineHome.catalogCount === 1,
    `offline catalog duplicated a conversation: ${offlineHome.catalogCount}`,
  );
  await page.screenshot({
    path: `${harness.artifactDir}/offline-catalog.png`,
    fullPage: true,
  });
  await page.locator("#mobile-offline-conversations [role='listitem']").first().click();
  await waitForReaderCount(60);
  assert((await readerState()).offline, "offline catalog did not open the cached reader");

  const recoveryAnchor = await visibleAnchor() || recoveryAnchorBefore;
  await page.context().setOffline(false);
  await harness.startServer();
  await page.evaluate(() => document.querySelector("#mobile-reader-revalidate").click());
  await page.waitForFunction(
    () => mobileConversationState.revalidating === false
      && mobileConversationState.loadingInitial === false
      && mobileConversationState.offline === false,
  );
  await page.waitForTimeout(250);
  await waitForCache(
    (value) => value.stats?.categoryCounts?.segments >= 4,
    "revalidation did not complete a cache write",
  );
  const recoveryAnchorAfter = await visibleAnchor();
  const recoveryTargetAfter = await page.evaluate((messageId) => {
    const scroll = document.querySelector("#mobile-reader-scroll");
    const node = document.querySelector(
      `#mobile-reader-messages [data-message-id="${CSS.escape(messageId)}"]`,
    );
    return node && scroll
      ? {
        messageId,
        offset: node.getBoundingClientRect().top - scroll.getBoundingClientRect().top,
      }
      : null;
  }, recoveryAnchor?.messageId);
  assert(
    recoveryTargetAfter?.messageId === recoveryAnchor?.messageId
      && Math.abs(recoveryAnchor.offset - recoveryTargetAfter.offset) <= 3,
    `revalidation drifted the visible anchor: ${JSON.stringify({
      recoveryAnchor,
      recoveryAnchorAfter,
      recoveryTargetAfter,
    })}`,
  );
  const recovery = await page.evaluate(async () => {
    const cached = await globalThis.yunosPrivateCache.getCachedConversationWindow({
      conversationId: mobileConversationState.conversationId,
      anchorMessageId: mobileConversationState.focusMessageId,
      touch: false,
    });
    return {
      offline: mobileConversationState.offline,
      validatedAt: cached.validatedAt,
      count: mobileConversationState.messages.length,
    };
  });
  assert(recovery.validatedAt >= onlineInspect.validatedAt, "online revalidation did not refresh validation time");

  const installFetchStub = (kind) => page.evaluate((stubKind) => {
    globalThis.__e2OriginalFetch ||= globalThis.fetch;
    globalThis.fetch = async (input, init) => {
      const url = new URL(String(input), location.href);
      if (!url.pathname.startsWith("/api/conversation-messages")) {
        return globalThis.__e2OriginalFetch(input, init);
      }
      if (stubKind === "404") {
        return new Response(JSON.stringify({ error: "synthetic missing" }), {
          status: 404,
          headers: { "content-type": "application/json" },
        });
      }
      if (stubKind === "empty") {
        return new Response(JSON.stringify({
          conversation: { conversation_id: "synthetic-empty", title: "Empty" },
          focus: { message_id: "synthetic-focus", position: 1, is_loaded: false },
          messages: [],
          cursors: { before: "", after: "" },
          has_before: false,
          has_after: false,
          limit: 20,
          mode: "anchor",
        }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ error: "synthetic gateway" }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    };
  }, kind);
  const resetAndLoad = (targetConversationId, targetMessageId) => page.evaluate(
    async ({ targetConversationId, targetMessageId }) => {
      resetMobileConversationState(targetConversationId, targetMessageId, "");
      mobileConversationState.activeKey = mobileConversationKey(
        targetConversationId,
        targetMessageId,
      );
      setMobileView("reader");
      await loadMobileConversationSegment("initial");
    },
    { targetConversationId, targetMessageId },
  );

  await installFetchStub("404");
  await resetAndLoad("synthetic-404", "synthetic-focus");
  const notFound = await readerState();
  assert(
    !notFound.offline && notFound.count === 0 && notFound.errors.some(
      (text) => text.includes("synthetic missing"),
    ),
    "HTTP 404 incorrectly fell back to cached content",
  );

  await installFetchStub("empty");
  await resetAndLoad("synthetic-empty", "synthetic-focus");
  const empty = await readerState();
  assert(
    !empty.offline && empty.count === 0 && empty.errors.length === 0,
    "HTTP 2xx empty response incorrectly fell back to cached content",
  );

  await installFetchStub("502");
  await resetAndLoad(conversationId, anchors.messageId);
  const gateway = await readerState();
  assert(
    gateway.offline && gateway.count >= 60,
    "HTTP 502 did not fall back to cached text",
  );
  await page.evaluate(() => {
    globalThis.fetch = globalThis.__e2OriginalFetch;
    delete globalThis.__e2OriginalFetch;
  });

  const apiMethods = [...new Set(apiRequests.map((request) => request.method))];
  assert(apiMethods.every((method) => method === "GET"), `non-GET API requests observed: ${apiMethods}`);
  assert(
    apiRequests.some((request) => request.url.includes("/api/message-context"))
      && apiRequests.some((request) => request.url.includes("/api/conversation-messages")),
    "both approved text API routes were not observed",
  );
  assert(pageErrors.length === 0, `page errors: ${pageErrors.join(" | ")}`);

  return {
    realAnchorPosition: anchors.position,
    previewMessages: preview.count,
    initialMessages: initial.count,
    onlineMessagesAfterPaging: online.count,
    cachedMessages: onlineInspect.cachedCount,
    cachedSegments: onlineInspect.inspect.stats.categoryCounts.segments,
    logicalBytes: onlineInspect.inspect.stats.totalBytes,
    storeCounts: onlineInspect.inspect.stats.categoryCounts,
    storeLogicalBytes: onlineInspect.inspect.stats.categoryBytes,
    offlineMessages: offline.count,
    offlineCatalogItems: offlineHome.catalogCount,
    offlineGapPlaceholders: offline.gaps,
    recoveryMessages: recovery.count,
    recoveryValidatedAt: recovery.validatedAt,
    fallback404: false,
    fallbackEmpty2xx: false,
    fallback502: gateway.offline,
    cacheStorageNames: onlineInspect.cacheNames,
    apiMethods,
    apiRequestCount: apiRequests.length,
    consoleErrors,
    screenshots: [
      `${harness.artifactDir}/online-reader.png`,
      `${harness.artifactDir}/offline-reader.png`,
      `${harness.artifactDir}/offline-catalog.png`,
    ],
  };
}
