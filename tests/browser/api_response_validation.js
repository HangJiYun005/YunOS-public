"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../web/app.js"), "utf8");
const start = source.indexOf("async function api(");
const end = source.indexOf("async function messageApi(", start);
assert(start >= 0 && end > start);

async function check(response, expectedError) {
  const context = vm.createContext({
    fetch: async () => response,
    window: { dispatchEvent() {} },
    CustomEvent: class {},
    t: (_key, _args, fallback) => fallback,
  });
  vm.runInContext(source.slice(start, end), context);
  const result = vm.runInContext('api("/api/status")', context);
  if (expectedError) {
    await assert.rejects(result, (error) => {
      assert.equal(error.status, response.status);
      assert.equal(error.technicalDetail, expectedError);
      return true;
    });
  } else {
    assert.equal((await result).ok, true);
  }
}

(async () => {
  await check({ ok: true, status: 200, json: async () => ({ ok: true }) });
  for (const status of [200, 502]) {
    await check({
      ok: status === 200, status,
      json: async () => { throw new SyntaxError("Unexpected HTML"); },
    }, "INVALID_JSON");
  }
  await check({ ok: false, status: 403, json: async () => ({ error: "forbidden" }) }, "forbidden");
  console.log("API response validation: 4 cases passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
