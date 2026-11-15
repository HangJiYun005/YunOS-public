async (page, harness = {}) => {
  const baseUrl = harness.baseUrl || "http://127.0.0.1:8782/";
  const requests = [];
  const failedRequests = [];
  const consoleErrors = [];
  const pageErrors = [];
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const waitUntil = async (predicate, message, timeout = 10000) => {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (await predicate()) return;
      await page.waitForTimeout(50);
    }
    throw new Error(message);
  };
  const searchRequests = (query, offset = null) => requests.filter((entry) => (
    entry.query === query && (offset === null || entry.offset === offset)
  ));
  const makeHit = (query, conversationId, index, suffix, semantic = false) => ({
    conversation_id: conversationId,
    title: `会话 ${index + 1}`,
    snippet: semantic
      ? `与当前主题相关的语义正文 ${index + 1}-${suffix}`
      : `\`\`\`text
          ### **这是包含 \`${query}\` 的正文命中 ${index + 1}-${suffix}**
          ---
          | 用于验证 [移动结果](https://example.test/result) | 正文优先 |
        \`\`\``,
    create_time_iso: `2026-07-${String(20 - (index % 12)).padStart(2, "0")}T08:00:00Z`,
    match_type: semantic ? "semantic" : "keyword",
  });
  const makeGroup = (query, offset, index, sort) => {
    const absoluteIndex = offset + index;
    const conversationId = `${query}-${sort}-${absoluteIndex}`;
    const semantic = index === 1;
    return {
      conversation_id: conversationId,
      title: `会话 ${absoluteIndex + 1}`,
      best_hit: makeHit(query, conversationId, absoluteIndex, "best", semantic),
      other_hits: [
        makeHit(query, conversationId, absoluteIndex, "other-1"),
        makeHit(query, conversationId, absoluteIndex, "other-2"),
      ],
      match_count: 3,
      other_match_count: 2,
    };
  };

  page.setDefaultTimeout(12000);
  page.on("console", (message) => {
    const expectedMock503 = message.text().includes("Failed to load resource")
      && message.text().includes("503");
    if (message.type() === "error" && !message.text().includes("favicon.ico") && !expectedMock503) {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    failedRequests.push({ url: request.url(), failure: request.failure()?.errorText || "failed" });
  });

  await page.addInitScript(() => {
    const originalAbort = AbortController.prototype.abort;
    window.__v82AbortCount = 0;
    AbortController.prototype.abort = function patchedAbort(...args) {
      window.__v82AbortCount += 1;
      return originalAbort.apply(this, args);
    };
    const visualViewportState = {
      width: window.innerWidth,
      height: window.innerHeight,
      offsetTop: 0,
      offsetLeft: 0,
    };
    const visualViewportTarget = new EventTarget();
    for (const propertyName of ["width", "height", "offsetTop", "offsetLeft"]) {
      Object.defineProperty(visualViewportTarget, propertyName, {
        configurable: true,
        get: () => visualViewportState[propertyName],
      });
    }
    window.__setVisualViewport = (values, eventType = "resize") => {
      Object.assign(visualViewportState, values);
      visualViewportTarget.dispatchEvent(new Event(eventType));
    };
    window.__setVisualViewportAvailability = (available) => {
      Object.defineProperty(window, "visualViewport", {
        configurable: true,
        value: available ? visualViewportTarget : undefined,
      });
    };
    window.__setVisualViewportAvailability(true);
    // The deterministic mock must see page requests directly. A pending
    // registration keeps this isolated browser session free of SW routing.
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

  let paginationFailureRemaining = 1;
  const searchRoute = async (route) => {
    const request = route.request();
    const params = Object.fromEntries(request.url().split("?", 2)[1].split("&").map((part) => {
      const [key, value = ""] = part.split("=", 2);
      return [decodeURIComponent(key), decodeURIComponent(value.replace(/\+/g, " "))];
    }));
    const query = params.q || "";
    const offset = Number(params.offset || 0);
    const limit = Number(params.limit || 20);
    const sort = params.sort || "relevance";
    requests.push({
      method: request.method(),
      query,
      offset,
      limit,
      sort,
      startedAt: Date.now(),
      url: request.url(),
    });

    if (query === "ab") await page.waitForTimeout(1500);
    else await page.waitForTimeout(25);

    if (query === "失败" && offset === 20 && paginationFailureRemaining > 0) {
      paginationFailureRemaining -= 1;
      await route.fulfill({
        status: 503,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify({ error: "mock pagination failure" }),
      });
      return;
    }

    const total = query === "失败" ? 40 : 80;
    const items = Array.from(
      { length: Math.max(0, Math.min(limit, total - offset)) },
      (_, index) => makeGroup(query, offset, index, sort),
    );
    try {
      await route.fulfill({
        status: 200,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify({
          items,
          total_conversations: total,
          has_more: offset + items.length < total,
          next_offset: offset + items.length,
          offset,
          limit,
          sort,
        }),
      });
    } catch {
      // An aborted fetch can close the route before the delayed mock responds.
    }
  };
  await page.route("**/api/search?**", searchRoute);

  const input = page.locator("#mobile-search-input");
  const form = page.locator("#mobile-search-form");
  const results = page.locator("#mobile-search-results");
  const initialGeometry = await form.boundingBox();
  assert(initialGeometry, "mobile search form is missing");
  await input.focus();
  await page.evaluate(() => {
    window.__setVisualViewport({
      width: 402,
      height: 520,
      offsetTop: 280,
      offsetLeft: 14,
    }, "resize");
    window.__setVisualViewport({ offsetTop: 300 }, "scroll");
  });
  await page.waitForTimeout(280);
  const focusedGeometry = await form.boundingBox();
  const focusedViewportState = await page.evaluate(() => {
    const shellRect = document.querySelector("#yunos-mobile-shell").getBoundingClientRect();
    const headerRect = document.querySelector(".yunos-mobile-header").getBoundingClientRect();
    const formRect = document.querySelector("#mobile-search-form").getBoundingClientRect();
    const viewport = window.visualViewport;
    return {
      viewport: {
        width: viewport.width,
        height: viewport.height,
        offsetTop: viewport.offsetTop,
        offsetLeft: viewport.offsetLeft,
      },
      shell: {
        top: shellRect.top,
        right: shellRect.right,
        bottom: shellRect.bottom,
        left: shellRect.left,
        width: shellRect.width,
        height: shellRect.height,
      },
      header: {
        top: headerRect.top,
        bottom: headerRect.bottom,
      },
      form: {
        top: formRect.top,
        bottom: formRect.bottom,
      },
      cssVariables: {
        width: getComputedStyle(document.querySelector("#yunos-mobile-shell")).getPropertyValue("--yunos-mobile-visual-width").trim(),
        height: getComputedStyle(document.querySelector("#yunos-mobile-shell")).getPropertyValue("--yunos-mobile-visual-height").trim(),
        offsetTop: getComputedStyle(document.querySelector("#yunos-mobile-shell")).getPropertyValue("--yunos-mobile-visual-offset-top").trim(),
        offsetLeft: getComputedStyle(document.querySelector("#yunos-mobile-shell")).getPropertyValue("--yunos-mobile-visual-offset-left").trim(),
      },
      scrollX: window.scrollX,
      scrollY: window.scrollY,
    };
  });
  const visualBottom = focusedViewportState.viewport.offsetTop + focusedViewportState.viewport.height;
  assert(
    focusedGeometry && focusedGeometry.y - focusedViewportState.viewport.offsetTop < initialGeometry.y - 100,
    "focused search did not move to the top of the visual viewport",
  );
  assert(Math.abs(focusedViewportState.shell.top - focusedViewportState.viewport.offsetTop) < 1, "mobile shell did not follow visual viewport offsetTop");
  assert(Math.abs(focusedViewportState.shell.left - focusedViewportState.viewport.offsetLeft) < 1, "mobile shell did not follow visual viewport offsetLeft");
  assert(Math.abs(focusedViewportState.shell.width - focusedViewportState.viewport.width) < 1, "mobile shell did not follow visual viewport width");
  assert(Math.abs(focusedViewportState.shell.height - focusedViewportState.viewport.height) < 1, "mobile shell did not follow visual viewport height");
  assert(focusedViewportState.header.top >= focusedViewportState.viewport.offsetTop - 1, "mobile header escaped above the visual viewport");
  assert(focusedViewportState.form.top >= focusedViewportState.header.bottom - 1, "focused search overlapped or escaped above the mobile header");
  assert(focusedViewportState.form.bottom <= visualBottom + 1, "focused search escaped below the visual viewport");
  assert(focusedViewportState.shell.bottom <= visualBottom + 1, "mobile shell escaped below the visual viewport");
  assert(focusedViewportState.cssVariables.width === "402px", "visual viewport width CSS variable is incorrect");
  assert(focusedViewportState.cssVariables.height === "520px", "visual viewport height CSS variable is incorrect");
  assert(focusedViewportState.cssVariables.offsetTop === "300px", "visual viewport offsetTop CSS variable is incorrect");
  assert(focusedViewportState.cssVariables.offsetLeft === "14px", "visual viewport offsetLeft CSS variable is incorrect");
  assert(focusedViewportState.scrollX === 0 && focusedViewportState.scrollY === 0, "visual viewport sync changed the page scroll position");
  await page.evaluate(() => {
    window.__setVisualViewport({
      width: 430,
      height: 932,
      offsetTop: 0,
      offsetLeft: 0,
    }, "resize");
  });
  await page.waitForTimeout(300);
  const dismissedFocusedState = await page.evaluate(() => {
    const formRect = document.querySelector("#mobile-search-form").getBoundingClientRect();
    const input = document.querySelector("#mobile-search-input");
    const shell = document.querySelector("#yunos-mobile-shell");
    return {
      inputStillFocused: document.activeElement === input,
      keyboardOpen: shell.dataset.keyboardOpen,
      searchPosition: shell.dataset.searchPosition,
      formY: formRect.y,
    };
  });
  assert(dismissedFocusedState.inputStillFocused, "keyboard-dismiss simulation unexpectedly blurred the input");
  assert(dismissedFocusedState.keyboardOpen === "false", "keyboard stayed marked open after the visual viewport expanded");
  assert(dismissedFocusedState.searchPosition === "center", "empty focused search did not reset after the keyboard closed");
  assert(Math.abs(dismissedFocusedState.formY - initialGeometry.y) < 3, "keyboard dismissal did not restore the centered search position");

  await page.evaluate(() => {
    window.__setVisualViewport({
      width: 402,
      height: 520,
      offsetTop: 300,
      offsetLeft: 14,
    }, "resize");
  });
  await page.waitForTimeout(280);
  const reopenedFocusedState = await page.evaluate(() => {
    const formRect = document.querySelector("#mobile-search-form").getBoundingClientRect();
    const shell = document.querySelector("#yunos-mobile-shell");
    return {
      keyboardOpen: shell.dataset.keyboardOpen,
      searchPosition: shell.dataset.searchPosition,
      relativeFormY: formRect.y - window.visualViewport.offsetTop,
    };
  });
  assert(reopenedFocusedState.keyboardOpen === "true", "keyboard reopen was not detected while the input retained focus");
  assert(reopenedFocusedState.searchPosition === "top", "search did not pin again when the keyboard reopened");
  assert(reopenedFocusedState.relativeFormY < 120, "reopened keyboard left the search away from the visual viewport top");

  await input.blur();
  await page.evaluate(() => {
    window.__setVisualViewport({
      width: 430,
      height: 932,
      offsetTop: 0,
      offsetLeft: 0,
    }, "resize");
  });
  await page.waitForTimeout(300);
  const restoredGeometry = await form.boundingBox();
  assert(restoredGeometry && Math.abs(restoredGeometry.y - initialGeometry.y) < 3, "empty blurred search did not restore its centered position");
  const fallbackViewportState = await page.evaluate(() => {
    window.__setVisualViewportAvailability(false);
    window.dispatchEvent(new Event("resize"));
    const shellStyle = getComputedStyle(document.querySelector("#yunos-mobile-shell"));
    const state = {
      width: shellStyle.getPropertyValue("--yunos-mobile-visual-width").trim(),
      height: shellStyle.getPropertyValue("--yunos-mobile-visual-height").trim(),
      offsetTop: shellStyle.getPropertyValue("--yunos-mobile-visual-offset-top").trim(),
      offsetLeft: shellStyle.getPropertyValue("--yunos-mobile-visual-offset-left").trim(),
    };
    window.__setVisualViewportAvailability(true);
    window.__setVisualViewport({
      width: 430,
      height: 932,
      offsetTop: 0,
      offsetLeft: 0,
    }, "resize");
    return state;
  });
  assert(fallbackViewportState.width === "430px", "innerWidth fallback is incorrect");
  assert(fallbackViewportState.height === "932px", "innerHeight fallback is incorrect");
  assert(fallbackViewportState.offsetTop === "0px", "offsetTop fallback is incorrect");
  assert(fallbackViewportState.offsetLeft === "0px", "offsetLeft fallback is incorrect");

  await input.fill("a");
  await page.waitForTimeout(1100);
  assert(searchRequests("a").length === 0, "one-character input emitted a search request");
  assert(await page.locator("#mobile-search-hint").textContent() === "再输入一个字开始搜索", "one-character hint is incorrect");

  const debounceStartedAt = Date.now();
  await input.fill("ab");
  await waitUntil(() => searchRequests("ab").length === 1, "debounced request did not start");
  const debounceDelay = searchRequests("ab")[0].startedAt - debounceStartedAt;
  assert(debounceDelay >= 950 && debounceDelay < 1400, `debounce timing drifted: ${debounceDelay}ms`);
  await input.fill("abc");
  await waitUntil(() => searchRequests("abc").length === 1, "replacement query did not start");
  await waitUntil(async () => (await results.locator(".yunos-mobile-result").count()) === 20, "replacement query did not render");
  const abortCountDuringRace = await page.evaluate(() => window.__v82AbortCount);
  assert(abortCountDuringRace >= 1, "new input did not abort the old request");
  assert((await results.textContent()).includes("abc"), "new query result is missing");
  assert(!(await results.textContent()).includes("包含 ab 的正文"), "stale query won the render race");

  const duplicateCount = searchRequests("abc").length;
  await input.press("Enter");
  await page.waitForTimeout(250);
  assert(searchRequests("abc").length === duplicateCount, "identical completed query was requested again");

  await input.fill("即");
  await page.waitForTimeout(100);
  assert(searchRequests("即").length === 0, "single CJK character emitted a request");
  const immediateStartedAt = Date.now();
  await input.fill("即时");
  await input.press("Enter");
  await waitUntil(() => searchRequests("即时").length === 1, "iOS-style search submit did not start");
  const immediateDelay = searchRequests("即时")[0].startedAt - immediateStartedAt;
  assert(immediateDelay < 500, `immediate submit waited for debounce: ${immediateDelay}ms`);
  await waitUntil(async () => (await results.locator(".yunos-mobile-result").count()) === 20, "immediate submit did not render");
  assert(await results.locator("mark").count() > 0, "raw keyword highlight is missing");
  assert(await results.locator(".yunos-mobile-related-match").count() > 0, "semantic result label is missing");
  const cleanedSnippets = await results.locator(".yunos-mobile-result-snippet").allTextContents();
  assert(cleanedSnippets.every((snippet) => !snippet.includes("`") && !/\s{2,}/u.test(snippet)), "mobile result snippet retained Markdown or whitespace noise");
  assert(cleanedSnippets.every((snippet) => !snippet.trimStart().startsWith("text ")), "mobile result snippet retained a code-fence language label");
  assert(cleanedSnippets.every((snippet) => !/(?:#{1,6}|\*{2,}|_{2,}|~{2,}|[-=]{3,}|\|)/u.test(snippet)), "mobile result snippet retained Markdown structure markers");
  assert(cleanedSnippets.some((snippet) => snippet.includes("移动结果") && !snippet.includes("example.test")), "mobile result link did not become plain text");

  const toggles = results.locator(".yunos-mobile-more-matches");
  await toggles.nth(0).click();
  assert(await toggles.nth(0).getAttribute("aria-expanded") === "true", "first conversation did not expand");
  await toggles.nth(1).click();
  assert(await toggles.nth(0).getAttribute("aria-expanded") === "false", "first conversation stayed expanded");
  assert(await toggles.nth(1).getAttribute("aria-expanded") === "true", "second conversation did not expand");
  assert(await results.locator(".yunos-mobile-other-matches:not([hidden])").count() === 1, "more than one conversation is expanded");

  await page.locator("#mobile-sort-toggle").click();
  await waitUntil(() => searchRequests("即时", 0).some((entry) => entry.sort === "latest"), "latest sort did not reload page one");
  await waitUntil(async () => (await page.locator("#mobile-sort-toggle").textContent()) === "最新优先", "latest sort label did not update");
  assert(await input.inputValue() === "即时", "sort switch lost the query");

  await page.locator("#mobile-search-clear").click();
  await page.waitForTimeout(300);
  const clearedGeometry = await form.boundingBox();
  assert(await input.inputValue() === "", "clear search did not empty the input");
  assert(await page.locator("#mobile-results-page").isHidden(), "clear search left results visible");
  assert(clearedGeometry && Math.abs(clearedGeometry.y - initialGeometry.y) < 3, "clear search did not restore the centered home state");

  await input.fill("加载");
  await input.press("Enter");
  await waitUntil(async () => (await results.locator(".yunos-mobile-result").count()) === 20, "loading test first page failed");
  const resultPage = page.locator("#mobile-results-page");
  const scrollToBottom = () => resultPage.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  for (let attempt = 0; attempt < 5 && searchRequests("加载", 40).length === 0; attempt += 1) {
    await scrollToBottom();
    await page.waitForTimeout(250);
  }
  await waitUntil(async () => (await results.locator(".yunos-mobile-result").count()) === 60, "automatic loading did not stop at 60 results", 15000);
  await page.waitForTimeout(500);
  assert(searchRequests("加载", 60).length === 0, "automatic loading crossed the 60-result boundary");
  const automaticOffsetsBeforeManual = searchRequests("加载").map((entry) => entry.offset);
  const loadMore = page.locator("#mobile-load-more");
  assert(await loadMore.isVisible(), "large manual load-more control is missing at 60");
  assert(await loadMore.textContent() === "加载更多", "manual load-more label is incorrect");
  await loadMore.click();
  await waitUntil(async () => (await results.locator(".yunos-mobile-result").count()) === 80, "manual load-more did not append results");
  assert(searchRequests("加载", 60).length === 1, "manual load-more did not request offset 60 exactly once");

  await input.fill("失败");
  await input.press("Enter");
  await waitUntil(async () => (await results.locator(".yunos-mobile-result").count()) === 20, "pagination failure test first page failed");
  await scrollToBottom();
  await waitUntil(() => searchRequests("失败", 20).length === 1, "pagination failure was not reached");
  await waitUntil(async () => (await page.locator("#mobile-search-status").textContent()).includes("加载失败"), "pagination failure message is missing");
  await page.waitForTimeout(500);
  assert(searchRequests("失败", 20).length === 1, "pagination failure retried automatically");
  assert(await loadMore.textContent() === "重新加载", "pagination retry control is missing");
  await loadMore.click();
  await waitUntil(async () => (await results.locator(".yunos-mobile-result").count()) === 40, "manual pagination retry failed");
  assert(searchRequests("失败", 20).length === 2, "pagination retry count is incorrect");

  const mobileNetwork = requests.slice();
  assert(mobileNetwork.every((entry) => entry.method === "GET"), "mobile search emitted a non-GET request");
  assert(mobileNetwork.every((entry) => entry.limit === 20), "mobile search page size changed from 20");
  assert(mobileNetwork.every((entry) => entry.url.includes("scope=all") && entry.url.includes("aggregate=conversation")), "mobile search lost its explicit read-only scope");

  const visibleTargetSizes = await page.locator("#yunos-mobile-shell button, #yunos-mobile-shell input").evaluateAll((nodes) => nodes
    .filter((node) => {
      const style = getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    })
    .map((node) => ({ label: node.getAttribute("aria-label") || node.textContent?.trim() || node.id, width: node.getBoundingClientRect().width, height: node.getBoundingClientRect().height })));
  assert(visibleTargetSizes.every((target) => target.width >= 43.5 && target.height >= 43.5), `touch target below 44px: ${JSON.stringify(visibleTargetSizes)}`);
  const portraitOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth || document.body.scrollWidth > document.body.clientWidth);
  assert(!portraitOverflow, "portrait mobile surface has horizontal overflow");

  await page.locator("#mobile-recent-button").click();
  await page.locator("#mobile-recent-drawer").waitFor({ state: "visible" });
  assert(await page.locator("#mobile-recent-searches-title").isVisible(), "recent-search section heading disappeared");
  assert(await page.locator("#mobile-recent-conversations-title").isVisible(), "recent-conversation section heading disappeared");
  const deleteButtons = page.locator("[aria-label^='删除最近搜索：']");
  const historyCount = await deleteButtons.count();
  await deleteButtons.first().click();
  assert(await deleteButtons.count() === historyCount - 1, "single recent-search deletion failed");
  await page.locator("#mobile-recent-clear").click();
  assert((await page.locator("#mobile-recent-searches").textContent()).includes("还没有最近搜索"), "clear history did not empty recent searches");
  const handle = page.locator(".yunos-mobile-drawer-handle");
  const handleBox = await handle.boundingBox();
  assert(handleBox, "drawer drag handle is missing");
  await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2 + 100, { steps: 5 });
  await page.mouse.up();
  await page.locator("#mobile-recent-drawer").waitFor({ state: "hidden" });

  await page.setViewportSize({ width: 932, height: 430 });
  await page.evaluate(() => {
    window.__setVisualViewport({
      width: 932,
      height: 430,
      offsetTop: 0,
      offsetLeft: 0,
    }, "resize");
  });
  await page.waitForTimeout(300);
  const landscapeState = await page.evaluate(() => {
    const mobile = document.querySelector("#yunos-mobile-shell");
    const desktop = document.querySelector("#yunos-desktop-shell");
    const formRect = document.querySelector("#mobile-search-form").getBoundingClientRect();
    return {
      mobileDisplay: getComputedStyle(mobile).display,
      desktopDisplay: getComputedStyle(desktop).display,
      formY: formRect.y,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth || document.body.scrollWidth > document.body.clientWidth,
    };
  });
  assert(landscapeState.mobileDisplay !== "none" && landscapeState.desktopDisplay === "none", "landscape switched to the desktop shell");
  assert(landscapeState.formY < 120, "low-height landscape search is not pinned near the top");
  assert(!landscapeState.overflow, "landscape mobile surface has horizontal overflow");

  await page.unroute("**/api/search?**", searchRoute);
  await page.setViewportSize({ width: 1200, height: 800 });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  const desktopState = await page.evaluate(() => ({
    desktopDisplay: getComputedStyle(document.querySelector("#yunos-desktop-shell")).display,
    mobileDisplay: getComputedStyle(document.querySelector("#yunos-mobile-shell")).display,
    mainCount: document.querySelectorAll("main").length,
    omniboxVisible: document.querySelector("#home-omnibox-form").getBoundingClientRect().width > 0,
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth || document.body.scrollWidth > document.body.clientWidth,
  }));
  assert(desktopState.desktopDisplay !== "none" && desktopState.mobileDisplay === "none", "desktop v8.1 shell regressed");
  assert(desktopState.mainCount === 1 && desktopState.omniboxVisible, "desktop v8.1 main/Omnibox contract regressed");
  assert(!desktopState.overflow, "desktop v8.1 surface has horizontal overflow");
  assert(pageErrors.length === 0, `page errors: ${JSON.stringify(pageErrors)}`);
  assert(consoleErrors.length === 0, `console errors: ${JSON.stringify(consoleErrors)}`);

  return {
    status: "GO",
    debounceDelay,
    immediateSubmitDelay: immediateDelay,
    abortCountDuringRace,
    failedRequests,
    requestCount: requests.length,
    pagination: {
      automaticOffsetsBeforeManual,
      manualOffset60Count: searchRequests("加载", 60).length,
      retryOffset20Count: searchRequests("失败", 20).length,
    },
    touchTargets: visibleTargetSizes,
    focusedViewportState,
    dismissedFocusedState,
    reopenedFocusedState,
    fallbackViewportState,
    landscapeState,
    desktopState,
  };
}
