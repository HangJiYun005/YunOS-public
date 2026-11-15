"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn, spawnSync } = require("child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..", "..");
const callbackPath = path.resolve(process.argv[2] || "");
const port = Number(process.argv[3] || 8782);
const baseUrl = `http://127.0.0.1:${port}/`;
const python = path.join(root, ".venv", "Scripts", "python.exe");
const serverScript = path.join(root, "tools", "local_web.py");
const sourceDatabaseOverride = process.env.YUNOS_BROWSER_SOURCE_DB
  ? path.resolve(process.env.YUNOS_BROWSER_SOURCE_DB)
  : "";
const database = sourceDatabaseOverride
  || path.join(root, "data", "ayunos_chatgpt.db");
const realReadonlySource = !sourceDatabaseOverride;
const cognitiveFixtureEnabled = process.env.YUNOS_BROWSER_COGNITIVE_FIXTURE === "1";
const blockServiceWorkers = process.env.YUNOS_BROWSER_BLOCK_SERVICE_WORKERS === "1";
const artifactRoot = process.env.YUNOS_BROWSER_ARTIFACT_DIR
  ? path.resolve(process.env.YUNOS_BROWSER_ARTIFACT_DIR)
  : path.join(root, "output", "playwright");
const artifactDir = path.join(
  artifactRoot,
  path.basename(callbackPath, path.extname(callbackPath)),
);

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function statusAvailable() {
  try {
    const response = await fetch(`${baseUrl}api/status`, {
      signal: AbortSignal.timeout(750),
    });
    return response.ok ? response.json() : null;
  } catch {
    return null;
  }
}

async function waitForServer(server, timeoutMs = 25_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (server.exitCode !== null) {
      throw new Error(`temporary server exited with ${server.exitCode}`);
    }
    const status = await statusAvailable();
    if (status) return status;
    await delay(150);
  }
  throw new Error("temporary server did not become ready");
}

async function stopServer(server) {
  if (!server || server.exitCode !== null) return;
  server.kill();
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline && server.exitCode === null) await delay(50);
  if (server.exitCode === null) server.kill("SIGKILL");
  const stoppedDeadline = Date.now() + 3000;
  while (Date.now() < stoppedDeadline) {
    if (!await statusAvailable()) return;
    await delay(100);
  }
  throw new Error(`temporary server still responds on ${baseUrl}`);
}

async function main() {
  if (!fs.existsSync(callbackPath)) {
    throw new Error(`browser callback not found: ${callbackPath}`);
  }
  fs.mkdirSync(artifactDir, { recursive: true });
  if (await statusAvailable()) {
    throw new Error(`refusing to replace an existing listener on ${baseUrl}`);
  }
  let server = null;
  let serverOutput = "";
  let fixtureTempRoot = null;
  let cognitiveFixture = null;
  let browser = null;
  try {
    if (cognitiveFixtureEnabled) {
    fixtureTempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "yunos-v10-e-browser-"));
    const fixtureDatabase = path.join(fixtureTempRoot, "operational-state.db");
    const prepared = spawnSync(
      python,
      [
        "-B",
        "-m",
        "tests.browser.prepare_v10_e_cognitive_fixture",
        "--output",
        fixtureDatabase,
      ],
      { cwd: root, encoding: "utf8", windowsHide: true },
    );
    if (prepared.status !== 0) {
      throw new Error(
        `cognitive fixture creation failed (${prepared.status}):\n${prepared.stderr || prepared.stdout}`,
      );
    }
    cognitiveFixture = JSON.parse(prepared.stdout.trim());
    fs.writeFileSync(
      path.join(artifactDir, "cognitive-fixture.json"),
      `${JSON.stringify(cognitiveFixture, null, 2)}\n`,
      "utf8",
    );
    }
    const serverInstances = [];
    const startServer = async () => {
    if (await statusAvailable()) {
      throw new Error(`refusing to replace an existing listener on ${baseUrl}`);
    }
    const serverArguments = [
        serverScript,
        "--db",
        database,
        "--readonly",
        "--host",
        "127.0.0.1",
        "--port",
        String(port),
      ];
    if (realReadonlySource) serverArguments.push("--real");
    if (cognitiveFixture) {
      serverArguments.push(
        "--operational-state-db",
        cognitiveFixture.database,
      );
    }
    server = spawn(
      python,
      serverArguments,
      {
        cwd: root,
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    server.stdout.on("data", (chunk) => { serverOutput += chunk.toString(); });
    server.stderr.on("data", (chunk) => { serverOutput += chunk.toString(); });
    const status = await waitForServer(server);
    if (
      status.service_mode !== (realReadonlySource ? "real-readonly" : "web")
      || status.bound_host !== "127.0.0.1"
      || Number(status.bound_port) !== port
    ) {
      throw new Error(
        `temporary server boundary mismatch: ${JSON.stringify(status)}`,
      );
    }
    serverInstances.push({
      serviceMode: status.service_mode,
      host: status.bound_host,
      port: status.bound_port,
      processId: status.process_id,
      serverInstanceId: status.server_instance_id,
    });
    return status;
    };
    const stopActiveServer = async () => {
      await stopServer(server);
      server = null;
    };
    await startServer();
    browser = await chromium.launch({ channel: "chrome", headless: true });
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      serviceWorkers: blockServiceWorkers ? "block" : "allow",
    });
    const page = await context.newPage();
    const source = fs.readFileSync(callbackPath, "utf8");
    const callback = (0, eval)(`(${source})`);
    const result = await callback(page, {
      baseUrl,
      stopServer: stopActiveServer,
      startServer,
      serverInstances,
      artifactDir,
      cognitiveFixture,
    });
    const report = {
      status: "PASS",
      callback: path.relative(root, callbackPath).replaceAll("\\", "/"),
      serverInstances,
      result,
    };
    fs.writeFileSync(
      path.join(artifactDir, "acceptance-result.json"),
      `${JSON.stringify(report, null, 2)}\n`,
      "utf8",
    );
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  } catch (error) {
    error.message = `${error.message}\nTemporary server output:\n${serverOutput.slice(-4000)}`;
    throw error;
  } finally {
    await browser?.close();
    await stopServer(server);
    server = null;
    if (fixtureTempRoot) {
      const resolvedTemp = path.resolve(os.tmpdir());
      const resolvedFixture = path.resolve(fixtureTempRoot);
      if (!resolvedFixture.startsWith(`${resolvedTemp}${path.sep}`)) {
        throw new Error("refusing to clean cognitive fixture outside the system temp directory");
      }
      fs.rmSync(resolvedFixture, { recursive: true, force: true });
    }
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
