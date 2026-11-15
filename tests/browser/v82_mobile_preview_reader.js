async (page, harness = {}) => {
  const baseUrl = harness.baseUrl || "http://127.0.0.1:8782/";
  const artifactRoot = harness.artifactDir
    || "E:/AyunOS/output/playwright/v8.2-c-conversation-reader";
  const consoleErrors = [];
  const pageErrors = [];
  const searchRequests = [];
  const contextRequests = [];
  const readerRequests = [];
  let beforeAttempts = 0;

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
  const screenshot = (name) => page.screenshot({
    path: `${artifactRoot}/${name}`,
    animations: "disabled",
  });
  const searchParamsFor = (requestUrl) => new Map(
    (requestUrl.split("?", 2)[1] || "")
      .split("&")
      .filter(Boolean)
      .map((part) => {
        const separator = part.indexOf("=");
        const key = separator >= 0 ? part.slice(0, separator) : part;
        const value = separator >= 0 ? part.slice(separator + 1) : "";
        return [
          decodeURIComponent(key.replace(/\+/g, " ")),
          decodeURIComponent(value.replace(/\+/g, " ")),
        ];
      }),
  );
  const isoFor = (position) => `2026-07-${String(1 + (position % 20)).padStart(2, "0")}T${String(position % 24).padStart(2, "0")}:15:00Z`;
  const makeReaderMessage = (position, conversationId = "reader-conversation") => {
    const isUser = position % 2 === 1;
    return {
      conversation_id: conversationId,
      message_id: `reader-message-${position}`,
      ordinal: position - 1,
      position,
      role: isUser ? "user" : "assistant",
      role_label: isUser ? "用户" : "AI",
      create_time_iso: isoFor(position),
      text: isUser
        ? `第 ${position} 条用户消息：我正在验证移动端会话阅读链路。请保留搜索状态、命中定位和可选择的长文本。`
        : `第 ${position} 条 AI 回复：这里是用于检查双向分段加载、稳定滚动与追溯信息的回答正文。`,
      source_platform: "chatgpt",
      source_platform_label: "ChatGPT",
      model_slug: "gpt-5.6-sol",
      default_model_slug: "gpt-5.6-sol",
      model_trace: {
        message_model_slug: "gpt-5.6-sol",
        conversation_default_model_slug: "gpt-5.6-sol",
        resolved_model_slug: "gpt-5.6-sol",
      },
      is_focus: position === 31,
      reference_uri: `ayunos://conversation/${conversationId}?message=reader-message-${position}`,
    };
  };
  const conversationPayload = (positions, mode, { hasBefore, hasAfter, before, after }) => ({
    conversation: {
      conversation_id: "reader-conversation",
      title: "移动阅读链路验收会话",
      update_time_iso: "2026-07-21T09:00:00Z",
      message_count: 60,
      total_message_count: 60,
      default_model_slug: "gpt-5.6-sol",
      source_platform: "chatgpt",
      source_platform_label: "ChatGPT",
    },
    focus: { message_id: "reader-message-31", ordinal: 30, position: 31, is_loaded: positions.includes(31) },
    messages: positions.map((position) => makeReaderMessage(position)),
    cursors: { before, after },
    has_before: hasBefore,
    has_after: hasAfter,
    limit: 20,
    mode,
  });
  const makeHit = (absoluteIndex, suffix = "best") => ({
    conversation_id: absoluteIndex === 0 ? "reader-conversation" : `conversation-${absoluteIndex}`,
    message_id: absoluteIndex === 0 && suffix === "best"
      ? "reader-message-31"
      : `search-message-${absoluteIndex}-${suffix}`,
    title: absoluteIndex === 0 ? "移动阅读链路验收会话" : `搜索会话 ${absoluteIndex + 1}`,
    snippet: absoluteIndex === 0
      ? "阅读链路 命中正文：点击展开附近消息，再从来源行打开完整会话。"
      : `阅读链路 搜索结果 ${absoluteIndex + 1}，用于验证列表加载与返回恢复。`,
    create_time_iso: isoFor(absoluteIndex + 1),
    match_type: "keyword",
  });
  const makeGroup = (absoluteIndex) => ({
    conversation_id: absoluteIndex === 0 ? "reader-conversation" : `conversation-${absoluteIndex}`,
    title: absoluteIndex === 0 ? "移动阅读链路验收会话" : `搜索会话 ${absoluteIndex + 1}`,
    best_hit: makeHit(absoluteIndex),
    other_hits: [makeHit(absoluteIndex, "other-1"), makeHit(absoluteIndex, "other-2")],
    match_count: 3,
    other_match_count: 2,
  });
  page.setDefaultTimeout(15000);
  page.on("console", (message) => {
    const expectedMockFailure = message.type() === "error"
      && message.text().includes("Failed to load resource")
      && message.text().includes("503");
    if (message.type() === "error" && !message.text().includes("favicon.ico") && !expectedMockFailure) {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.unrouteAll({ behavior: "ignoreErrors" }).catch(() => {});
  await page.addInitScript(() => {
    ServiceWorkerContainer.prototype.register = () => new Promise(() => {});
  });
  await page.setViewportSize({ width: 430, height: 932 });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.evaluate(async () => {
    localStorage.removeItem("yunos.mobileSearch.history.v1");
    for (const registration of await navigator.serviceWorker.getRegistrations()) {
      await registration.unregister();
    }
    for (const key of await caches.keys()) await caches.delete(key);
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.route("**/api/search?**", async (route) => {
    const request = route.request();
    const params = searchParamsFor(request.url());
    const offset = Number(params.get("offset") || 0);
    const limit = Number(params.get("limit") || 20);
    searchRequests.push({
      method: request.method(),
      query: params.get("q") || "",
      sort: params.get("sort") || "relevance",
      offset,
    });
    const items = Array.from(
      { length: Math.max(0, Math.min(limit, 40 - offset)) },
      (_, index) => makeGroup(offset + index),
    );
    await page.waitForTimeout(35);
    await route.fulfill({
      status: 200,
      contentType: "application/json; charset=utf-8",
      body: JSON.stringify({
        items,
        total_conversations: 40,
        has_more: offset + items.length < 40,
        next_offset: offset + items.length,
        offset,
        limit,
        sort: params.get("sort") || "relevance",
      }),
    });
  });
  await page.route("**/api/message-context?**", async (route) => {
    const request = route.request();
    const params = searchParamsFor(request.url());
    contextRequests.push({
      method: request.method(),
      conversationId: params.get("conversation_id"),
      messageId: params.get("message_id"),
      window: params.get("window"),
    });
    const messages = [29, 30, 31, 32, 33].map((position) => makeReaderMessage(position));
    await page.waitForTimeout(80);
    await route.fulfill({
      status: 200,
      contentType: "application/json; charset=utf-8",
      body: JSON.stringify({
        conversation_id: "reader-conversation",
        conversation_title: "移动阅读链路验收会话",
        focus_message_id: "reader-message-31",
        window: 2,
        messages,
      }),
    });
  });
  await page.route("**/api/conversation-messages?**", async (route) => {
    const request = route.request();
    const params = searchParamsFor(request.url());
    const mode = params.has("before")
      ? "before"
      : (params.has("after") ? "after" : "anchor");
    readerRequests.push({ method: request.method(), mode, url: request.url() });
    if (mode === "before") {
      beforeAttempts += 1;
      await page.waitForTimeout(beforeAttempts === 1 ? 80 : 320);
      if (beforeAttempts === 1) {
        await route.fulfill({
          status: 503,
          contentType: "application/json; charset=utf-8",
          body: JSON.stringify({ error: "mock earlier-page failure" }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify(conversationPayload(
          Array.from({ length: 20 }, (_, index) => index + 1),
          "before",
          { hasBefore: false, hasAfter: true, before: null, after: "after-20" },
        )),
      });
      return;
    }
    if (mode === "after") {
      await page.waitForTimeout(120);
      await route.fulfill({
        status: 200,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify(conversationPayload(
          Array.from({ length: 20 }, (_, index) => index + 41),
          "after",
          { hasBefore: true, hasAfter: false, before: "before-41", after: null },
        )),
      });
      return;
    }
    await page.waitForTimeout(100);
    await route.fulfill({
      status: 200,
      contentType: "application/json; charset=utf-8",
      body: JSON.stringify(conversationPayload(
        Array.from({ length: 20 }, (_, index) => index + 21),
        "anchor",
        { hasBefore: true, hasAfter: true, before: "before-21", after: "after-40" },
      )),
    });
  });

  const input = page.locator("#mobile-search-input");
  const resultsPage = page.locator("#mobile-results-page");
  const results = page.locator("#mobile-search-results");
  await input.fill("阅读链路");
  await input.press("Enter");
  await waitUntil(
    async () => (await results.locator(".yunos-mobile-result").count()) === 20,
    "search results did not render",
  );
  await screenshot("01-search-results-default.png");

  const firstResult = results.locator(".yunos-mobile-result").first();
  const firstBody = firstResult.locator(".yunos-mobile-result-body").first();
  const firstSource = firstResult.locator(".yunos-mobile-result-source").first();
  const firstBodyBox = await firstBody.boundingBox();
  const firstSourceBox = await firstSource.boundingBox();
  assert(firstBodyBox && firstBodyBox.height >= 71.5, "result body target is below 72px");
  assert(firstSourceBox && firstSourceBox.height >= 47.5, "result source target is below 48px");
  assert(firstBodyBox.y + firstBodyBox.height <= firstSourceBox.y + 0.5, "result body and source zones overlap");

  await page.locator("#mobile-sort-toggle").click();
  await waitUntil(
    () => searchRequests.some((entry) => entry.sort === "latest" && entry.offset === 0),
    "latest sort did not reload",
  );
  await waitUntil(
    async () => !(await page.locator("#mobile-search-status").textContent()).includes("正在搜索"),
    "latest sort did not finish rendering",
  );
  await firstBody.click();
  await waitUntil(
    async () => (await firstResult.locator(".yunos-mobile-preview-message").count()) === 5,
    "five-message context preview did not render",
  );
  assert(contextRequests.length === 1, "preview request count is incorrect");
  assert(contextRequests[0].method === "GET", "preview request was not GET");
  assert(contextRequests[0].conversationId === "reader-conversation", "preview lost conversation anchor");
  assert(contextRequests[0].messageId === "reader-message-31", "preview lost message anchor");
  assert(contextRequests[0].window === "2", "preview window is not two messages each side");
  const previewBubbleWidths = await firstResult.locator(".yunos-mobile-message-bubble").evaluateAll(
    (nodes) => nodes.map((node) => node.getBoundingClientRect().width),
  );
  assert(previewBubbleWidths.length === 5, "preview bubbles are missing");
  await screenshot("02-nearby-message-preview.png");

  await firstResult.locator(".yunos-mobile-message-bubble").first().click();
  assert(await page.locator("#mobile-conversation-reader").isHidden(), "clicking a preview bubble opened the full reader");
  assert(
    await firstResult.locator(".yunos-mobile-context-preview:not([hidden])").count() === 1,
    "clicking a preview bubble collapsed the preview",
  );
  await firstBody.click();
  await waitUntil(
    async () => (await firstResult.locator(".yunos-mobile-context-preview:not([hidden])").count()) === 0,
    "clicking the result body did not collapse the preview",
  );
  await firstBody.click();
  await waitUntil(
    async () => (await firstResult.locator(".yunos-mobile-preview-message").count()) === 5,
    "cached preview did not reopen",
  );
  assert(contextRequests.length === 1, "reopening a cached preview refetched context");
  const secondResult = results.locator(".yunos-mobile-result").nth(1);
  await secondResult.locator(".yunos-mobile-result-body").first().click();
  await waitUntil(
    async () => (await secondResult.locator(".yunos-mobile-preview-message").count()) === 5,
    "second context preview did not render",
  );
  assert(
    await results.locator(".yunos-mobile-context-preview:not([hidden])").count() === 1
      && await firstResult.locator(".yunos-mobile-context-preview:not([hidden])").count() === 0,
    "more than one context preview remained expanded",
  );
  await firstBody.click();
  await waitUntil(
    async () => (await firstResult.locator(".yunos-mobile-preview-message").count()) === 5,
    "first context preview did not reopen after switching previews",
  );
  assert(
    await results.locator(".yunos-mobile-context-preview:not([hidden])").count() === 1,
    "preview switching left multiple previews expanded",
  );

  await firstResult.locator(".yunos-mobile-more-matches").click();
  await resultsPage.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitUntil(
    async () => (await results.locator(".yunos-mobile-result").count()) === 40,
    "second search page did not load",
  );
  await firstSource.scrollIntoViewIfNeeded();
  const savedResultsScrollTop = await resultsPage.evaluate((node) => node.scrollTop);
  const searchesBeforeReader = searchRequests.length;
  await firstSource.click();
  const reader = page.locator("#mobile-conversation-reader");
  const readerScroll = page.locator("#mobile-reader-scroll");
  const readerMessages = page.locator("#mobile-reader-messages");
  await waitUntil(
    async () => (await readerMessages.locator(".yunos-mobile-reader-message").count()) === 20,
    "initial reader segment did not render",
  );
  assert(await reader.isVisible(), "mobile reader is not visible");
  assert(readerRequests.length === 1 && readerRequests[0].mode === "anchor", "initial anchor request is incorrect");
  const focus = readerMessages.locator('[data-message-id="reader-message-31"]');
  assert(await focus.count() === 1, "focus message is not loaded exactly once");
  const positionsBeforeFocus = await readerMessages.locator(".yunos-mobile-reader-message").evaluateAll(
    (nodes) => nodes.findIndex((node) => node.dataset.messageId === "reader-message-31"),
  );
  assert(positionsBeforeFocus === 10, `initial focus is not centered: ${positionsBeforeFocus} messages before it`);
  const focusOffset = await focus.evaluate((node) => {
    const scroll = document.querySelector("#mobile-reader-scroll");
    const scrollRect = scroll.getBoundingClientRect();
    const rect = node.getBoundingClientRect();
    return { top: rect.top - scrollRect.top, height: scroll.clientHeight };
  });
  assert(focusOffset.top >= 80 && focusOffset.top <= focusOffset.height * 0.42, "focus message lacks retained context above it");
  await screenshot("03-full-conversation-focus.png");

  await readerScroll.evaluate((node) => { node.scrollTop = 0; });
  await waitUntil(() => beforeAttempts === 1, "earlier-page request did not start");
  await waitUntil(
    async () => (await page.locator("#mobile-reader-top-status").textContent()).includes("更早消息加载失败"),
    "earlier-page failure is not visible",
  );
  const topStatusText = await page.locator("#mobile-reader-top-status").textContent();
  assert(!topStatusText.includes("已到达会话开头"), "failure was misreported as the beginning");
  const retry = page.locator("#mobile-reader-top-status .yunos-mobile-reader-retry");
  await retry.click();
  await waitUntil(() => beforeAttempts === 2, "earlier-page retry did not start");
  const preservedMessage = readerMessages.locator('[data-message-id="reader-message-21"]');
  const preservedTopBefore = await preservedMessage.evaluate((node) => node.getBoundingClientRect().top);
  await waitUntil(
    async () => (await readerMessages.locator(".yunos-mobile-reader-message").count()) === 40,
    "earlier-page retry did not prepend messages",
  );
  const preservedTopAfter = await preservedMessage.evaluate((node) => node.getBoundingClientRect().top);
  const prependDrift = Math.abs(preservedTopAfter - preservedTopBefore);
  assert(prependDrift <= 2, `prepend visual position drifted by ${prependDrift}px`);
  assert((await page.locator("#mobile-reader-top-status").textContent()).includes("已到达会话开头"), "reader did not show the real beginning");
  await screenshot("04-after-upward-load.png");

  await readerScroll.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitUntil(
    async () => (await readerMessages.locator(".yunos-mobile-reader-message").count()) === 60,
    "later messages did not append",
  );
  assert((await page.locator("#mobile-reader-bottom-status").textContent()).includes("已到达会话结尾"), "reader did not show the real ending");

  await page.locator("#mobile-reader-hide-topbar").click();
  const restoreTopbar = page.locator("#mobile-reader-restore-topbar");
  await restoreTopbar.waitFor({ state: "visible" });
  const restoreBox = await restoreTopbar.boundingBox();
  assert(restoreBox && restoreBox.width >= 43.5 && restoreBox.height >= 43.5, "topbar restore target is below 44px");
  assert(restoreBox.x > 430 / 2, "restore control blocks the iPhone left edge");
  await screenshot("05-topbar-hidden.png");
  await restoreTopbar.click();
  await page.locator("#mobile-reader-topbar").waitFor({ state: "visible" });

  const focusBubble = focus.locator(".yunos-mobile-reader-bubble");
  await focusBubble.evaluate((bubble) => {
    const textNode = bubble.querySelector(".yunos-mobile-reader-text")?.firstChild;
    const range = document.createRange();
    range.selectNodeContents(textNode || bubble);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    bubble.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  assert(await focus.locator(".yunos-mobile-reader-trace").isHidden(), "text selection accidentally expanded trace metadata");
  await page.evaluate(() => window.getSelection()?.removeAllRanges());
  await focusBubble.click();
  await focus.locator(".yunos-mobile-reader-trace").waitFor({ state: "visible" });
  const traceText = await focus.locator(".yunos-mobile-reader-trace").textContent();
  assert(traceText.includes("模型 gpt-5.6-sol") && traceText.includes("消息 #31"), "trace metadata is incomplete");
  await screenshot("06-message-trace-expanded.png");

  const readingAnchor = readerMessages.locator('[data-message-id="reader-message-45"]');
  await readingAnchor.scrollIntoViewIfNeeded();
  await page.waitForTimeout(80);
  const savedReaderScrollTop = await readerScroll.evaluate((node) => node.scrollTop);
  await page.locator("#mobile-reader-back").click();
  await page.locator("#mobile-results-page").waitFor({ state: "visible" });
  const restoredResultsScrollTop = await resultsPage.evaluate((node) => node.scrollTop);
  assert(Math.abs(restoredResultsScrollTop - savedResultsScrollTop) <= 2, "search result scroll position was not restored");
  assert(await input.inputValue() === "阅读链路", "search query was not restored");
  assert(await page.locator("#mobile-sort-toggle").textContent() === "最新优先", "search sort was not restored");
  assert(await results.locator(".yunos-mobile-result").count() === 40, "loaded search results were discarded");
  assert(await firstResult.locator(".yunos-mobile-more-matches").getAttribute("aria-expanded") === "true", "expanded multi-match state was not restored");
  assert(
    await firstResult.locator(".yunos-mobile-context-preview:not([hidden])").count() === 1,
    "expanded preview was not restored",
  );
  assert(searchRequests.length === searchesBeforeReader, "returning to results repeated the search request");
  await screenshot("07-search-results-restored.png");

  const readerRequestsBeforeReopen = readerRequests.length;
  await firstSource.click();
  await reader.waitFor({ state: "visible" });
  await page.waitForTimeout(100);
  const reopenedReaderScrollTop = await readerScroll.evaluate((node) => node.scrollTop);
  assert(readerRequests.length === readerRequestsBeforeReopen, "reopening the same reader refetched its anchor page");
  assert(Math.abs(reopenedReaderScrollTop - savedReaderScrollTop) <= 2, "reader position was not restored");
  assert(await focus.locator(".yunos-mobile-reader-trace").isVisible(), "expanded message trace was not restored");

  await page.setViewportSize({ width: 430, height: 667 });
  await page.waitForTimeout(150);
  const lowHeight = await page.evaluate(() => ({
    mobileDisplay: getComputedStyle(document.querySelector("#yunos-mobile-shell")).display,
    readerDisplay: getComputedStyle(document.querySelector("#mobile-conversation-reader")).display,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth
      || document.body.scrollWidth > document.body.clientWidth,
  }));
  assert(lowHeight.mobileDisplay !== "none" && lowHeight.readerDisplay !== "none", "low-height portrait left the mobile reader");
  assert(!lowHeight.overflow, "low-height portrait has horizontal overflow");

  await page.setViewportSize({ width: 932, height: 430 });
  await page.waitForTimeout(200);
  const landscape = await page.evaluate(() => ({
    mobileDisplay: getComputedStyle(document.querySelector("#yunos-mobile-shell")).display,
    readerDisplay: getComputedStyle(document.querySelector("#mobile-conversation-reader")).display,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth
      || document.body.scrollWidth > document.body.clientWidth,
  }));
  assert(landscape.mobileDisplay !== "none" && landscape.readerDisplay !== "none", "landscape left the mobile reader");
  assert(!landscape.overflow, "landscape reader has horizontal overflow");
  await screenshot("08-landscape-reader.png");

  const portraitOverflow = await page.evaluate(() => {
    const scroll = document.querySelector("#mobile-reader-scroll");
    return scroll.scrollWidth > scroll.clientWidth;
  });
  assert(!portraitOverflow, "reader message viewport has horizontal overflow");
  const allApiRequests = [...searchRequests, ...contextRequests, ...readerRequests];
  assert(allApiRequests.every((entry) => entry.method === "GET"), `non-GET API request detected: ${JSON.stringify(allApiRequests)}`);
  assert(pageErrors.length === 0, `page errors: ${JSON.stringify(pageErrors)}`);
  assert(consoleErrors.length === 0, `console errors: ${JSON.stringify(consoleErrors)}`);

  await page.setViewportSize({ width: 1200, height: 800 });
  await page.waitForTimeout(150);
  const desktop = await page.evaluate(() => ({
    desktopDisplay: getComputedStyle(document.querySelector("#yunos-desktop-shell")).display,
    mobileDisplay: getComputedStyle(document.querySelector("#yunos-mobile-shell")).display,
    mainCount: document.querySelectorAll("main").length,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth
      || document.body.scrollWidth > document.body.clientWidth,
  }));
  assert(desktop.desktopDisplay !== "none" && desktop.mobileDisplay === "none", "desktop v8.1 shell regressed");
  assert(desktop.mainCount === 1 && !desktop.overflow, "desktop v8.1 layout regressed");

  return {
    status: "GO",
    screenshots: [
      "01-search-results-default.png",
      "02-nearby-message-preview.png",
      "03-full-conversation-focus.png",
      "04-after-upward-load.png",
      "05-topbar-hidden.png",
      "06-message-trace-expanded.png",
      "07-search-results-restored.png",
      "08-landscape-reader.png",
    ],
    requests: {
      search: searchRequests.length,
      context: contextRequests.length,
      reader: readerRequests.length,
      allGet: allApiRequests.every((entry) => entry.method === "GET"),
    },
    initialMessagesBeforeFocus: positionsBeforeFocus,
    prependDrift,
    restoredResultsScrollTop,
    savedResultsScrollTop,
    restoredReaderScrollTop: reopenedReaderScrollTop,
    savedReaderScrollTop,
    lowHeight,
    landscape,
    desktop,
  };
}
