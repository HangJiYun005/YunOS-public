async (page) => {
  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }
  const formal = JSON.parse(await page.evaluate(() =>
    localStorage.getItem("yunos.v81d.acceptance.formal"),
  ));
  assert(formal?.executePosts === 1, "formal execution evidence missing");
  const detailSummary = page.locator(".yunos-ai-disclosure > summary").filter({ hasText: /^执行详情/ });
  const details = detailSummary.locator("..");
  if ((await details.getAttribute("open")) === null) await detailSummary.click();
  const detailText = await details.innerText();
  assert(detailText.includes(formal.modelRunId), "run identity missing from result");
  assert(detailText.includes(formal.artifactIdentity), "artifact missing from result");
  assert(detailText.includes(formal.receiptIdentity), "receipt missing from result");
  assert(detailText.includes(formal.finishReason), "finish_reason missing from result");
  assert(detailText.includes(String(formal.usage.total_tokens)), "usage missing from result");

  const sourceSummary = page.locator(".yunos-ai-disclosure > summary").filter({ hasText: /^来源/ });
  const sources = sourceSummary.locator("..");
  if ((await sources.getAttribute("open")) === null) await sourceSummary.click();
  const firstSource = sources.locator(".yunos-ai-source-button").first();
  const conversationId = await firstSource.getAttribute("data-source-conversation-id");
  const messageId = await firstSource.getAttribute("data-source-message-id");
  assert(conversationId && messageId, "source anchor incomplete");
  await firstSource.click();
  await page.locator('#yunos-desktop-shell[data-page-state="workspace"]').waitFor();
  const message = page.locator(`.message[data-message-id="${messageId}"]`);
  await message.waitFor({ state: "visible" });
  await page.waitForTimeout(150);
  assert(await message.evaluate((node) => node.classList.contains("selected-message")), "source message not selected");
  assert(decodeURIComponent((page.url().split("#")[1] || "")) === messageId, "source hash wrong");
  await page.getByRole("button", { name: "返回 AI 结果" }).click();
  await page.locator('#home-ai-document[data-ai-state="success"]').waitFor({ state: "visible" });
  formal.sourceAnchor = { conversationId, messageId, opened: true };
  await page.evaluate((value) => {
    localStorage.setItem("yunos.v81d.acceptance.formal", JSON.stringify(value));
  }, formal);
  return formal.sourceAnchor;
}
