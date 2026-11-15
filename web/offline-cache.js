(() => {
  "use strict";

  const KIB = 1024;
  const MIB = 1024 * KIB;
  const GIB = 1024 * MIB;
  const DAY_MS = 24 * 60 * 60 * 1000;
  const TTL_MS = 30 * DAY_MS;
  const DEFAULT_DB_NAME = "yunos-private-content";
  const RESUME_DB_NAME = "yunos-mobile-resume";
  const DB_VERSION = 1;
  const RESUME_DB_VERSION = 1;
  const CONTENT_SCHEMA_VERSION = "v8.2-e1-1";
  const SEARCH_SNAPSHOT_VERSION = 1;
  const RESUME_STATE_VERSION = 1;
  const RESUME_STATE_KEY = "mobile-ui";
  const RESUME_STORE = "resumeStates";
  const RESUME_TTL_MS = DAY_MS;
  const DEFAULT_TARGET_BYTES = 256 * MIB;
  const DEFAULT_CEILING_BYTES = 512 * MIB;
  const PRODUCT_TARGET_BYTES = 1 * GIB;
  const PRODUCT_CEILING_BYTES = 2 * GIB;
  const DEFAULT_LEASE_MS = 15_000;
  const ACTIVE_READER_PROTECTION_MS = 30_000;
  const DEFAULT_BATCH_SIZE = 50;
  const DEFAULT_SEARCH_BATCH_SIZE = 100;
  const CLOCK_ROLLBACK_TOLERANCE_MS = 5 * 60 * 1000;
  const META_STORE = "meta";
  const LEGACY_MOBILE_HISTORY_KEY = "yunos.mobileSearch.history.v1";
  const DATA_STORES = Object.freeze([
    "conversations",
    "messages",
    "segments",
    "searchSnapshots",
    "thumbnails",
    "mediaObjects",
  ]);
  const STORE_DEFINITIONS = Object.freeze({
    meta: Object.freeze({ keyPath: "key", indexes: Object.freeze([]) }),
    conversations: Object.freeze({
      keyPath: "conversationId",
      indexes: Object.freeze([
        Object.freeze(["by_expires_at", "expiresAt"]),
        Object.freeze(["by_last_accessed_at", "lastAccessedAt"]),
      ]),
    }),
    messages: Object.freeze({
      keyPath: Object.freeze(["conversationId", "messageId"]),
      indexes: Object.freeze([
        Object.freeze(["by_conversation_id", "conversationId"]),
        Object.freeze(["by_expires_at", "expiresAt"]),
        Object.freeze(["by_last_accessed_at", "lastAccessedAt"]),
      ]),
    }),
    segments: Object.freeze({
      keyPath: "segmentKey",
      indexes: Object.freeze([
        Object.freeze(["by_conversation_id", "conversationId"]),
        Object.freeze(["by_expires_at", "expiresAt"]),
        Object.freeze(["by_last_accessed_at", "lastAccessedAt"]),
      ]),
    }),
    searchSnapshots: Object.freeze({
      keyPath: "snapshotKey",
      indexes: Object.freeze([
        Object.freeze(["by_expires_at", "expiresAt"]),
        Object.freeze(["by_last_accessed_at", "lastAccessedAt"]),
      ]),
    }),
    thumbnails: Object.freeze({
      keyPath: "mediaKey",
      indexes: Object.freeze([
        Object.freeze(["by_expires_at", "expiresAt"]),
        Object.freeze(["by_last_accessed_at", "lastAccessedAt"]),
      ]),
    }),
    mediaObjects: Object.freeze({
      keyPath: "mediaKey",
      indexes: Object.freeze([
        Object.freeze(["by_kind", "kind"]),
        Object.freeze(["by_expires_at", "expiresAt"]),
        Object.freeze(["by_last_accessed_at", "lastAccessedAt"]),
      ]),
    }),
  });
  const CLEANUP_PHASES = Object.freeze([
    Object.freeze({ id: "expired", category: "*" }),
    Object.freeze({ id: "attachments", category: "mediaObjects", kind: "attachment" }),
    Object.freeze({ id: "originals", category: "mediaObjects", kind: "original" }),
    Object.freeze({ id: "thumbnails", category: "thumbnails" }),
    Object.freeze({ id: "searchSnapshots", category: "searchSnapshots" }),
    Object.freeze({ id: "segments", category: "segments" }),
    Object.freeze({ id: "messages", category: "messages", orphanOnly: true }),
  ]);

  class PrivateCacheError extends Error {
    constructor(message, code = "private_cache_error", options = {}) {
      super(message, options);
      this.name = "PrivateCacheError";
      this.code = code;
    }
  }

  class LeaseBusyError extends PrivateCacheError {
    constructor(message = "Private cache writer lease is held by another page.") {
      super(message, "writer_lease_busy");
      this.name = "LeaseBusyError";
    }
  }

  class CapacityError extends PrivateCacheError {
    constructor(message = "Private cache effective ceiling would be exceeded.") {
      super(message, "effective_ceiling_exceeded");
      this.name = "CapacityError";
    }
  }

  function requestResult(request) {
    return new Promise((resolve, reject) => {
      request.addEventListener("success", () => resolve(request.result), { once: true });
      request.addEventListener("error", () => {
        reject(request.error || new PrivateCacheError("IndexedDB request failed."));
      }, { once: true });
    });
  }

  function transactionResult(transaction) {
    return new Promise((resolve, reject) => {
      transaction.addEventListener("complete", () => resolve(), { once: true });
      transaction.addEventListener("abort", () => {
        reject(transaction.error || new PrivateCacheError(
          "IndexedDB transaction was aborted.",
          "transaction_aborted",
        ));
      }, { once: true });
      transaction.addEventListener("error", () => {
        reject(transaction.error || new PrivateCacheError(
          "IndexedDB transaction failed.",
          "transaction_failed",
        ));
      }, { once: true });
    });
  }

  function deleteDatabase(indexedDb, name) {
    return new Promise((resolve, reject) => {
      const request = indexedDb.deleteDatabase(name);
      request.addEventListener("success", () => resolve({ deleted: true, blocked: false }), {
        once: true,
      });
      request.addEventListener("blocked", () => {
        reject(new PrivateCacheError(
          `Cannot clear ${name} while another page keeps it open.`,
          "delete_database_blocked",
        ));
      }, { once: true });
      request.addEventListener("error", () => {
        reject(request.error || new PrivateCacheError(
          `Failed to clear ${name}.`,
          "delete_database_failed",
        ));
      }, { once: true });
    });
  }

  function keyPathMatches(actual, expected) {
    if (Array.isArray(expected)) {
      return Array.from(actual || []).join("\u0000") === expected.join("\u0000");
    }
    return actual === expected;
  }

  function stableOwnerId(scope) {
    if (scope.crypto?.randomUUID) return scope.crypto.randomUUID();
    const random = scope.crypto?.getRandomValues
      ? Array.from(scope.crypto.getRandomValues(new Uint32Array(4)), (value) => value.toString(16))
      : [Math.random().toString(16).slice(2), Date.now().toString(16)];
    return `owner-${random.join("-")}`;
  }

  function emptyStats(at) {
    return {
      key: "stats",
      totalBytes: 0,
      categoryBytes: Object.fromEntries(DATA_STORES.map((name) => [name, 0])),
      categoryCounts: Object.fromEntries(DATA_STORES.map((name) => [name, 0])),
      updatedAt: at,
      lastReconciledAt: 0,
    };
  }

  function normalizedStats(value, at) {
    const base = emptyStats(at);
    if (!value || typeof value !== "object") return base;
    base.totalBytes = Math.max(0, Number(value.totalBytes) || 0);
    for (const category of DATA_STORES) {
      base.categoryBytes[category] = Math.max(
        0,
        Number(value.categoryBytes?.[category]) || 0,
      );
      base.categoryCounts[category] = Math.max(
        0,
        Number(value.categoryCounts?.[category]) || 0,
      );
    }
    base.updatedAt = Number(value.updatedAt) || at;
    base.lastReconciledAt = Number(value.lastReconciledAt) || 0;
    return base;
  }

  function cloneForWrite(scope, value) {
    if (typeof scope.structuredClone === "function") return scope.structuredClone(value);
    return value;
  }

  function logicalByteSize(scope, value, seen = new Set(), keyName = "") {
    if (value === null || value === undefined) return 0;
    if (keyName === "byteSize") return 0;
    if (typeof value === "string") return new TextEncoder().encode(value).byteLength;
    if (typeof value === "number") return 8;
    if (typeof value === "boolean") return 1;
    if (typeof value === "bigint") return 8;
    if (value instanceof Date) return logicalByteSize(scope, value.toISOString(), seen);
    if (scope.Blob && value instanceof scope.Blob) return value.size;
    if (value instanceof ArrayBuffer) return value.byteLength;
    if (ArrayBuffer.isView(value)) return value.byteLength;
    if (typeof value !== "object") return 0;
    if (seen.has(value)) {
      throw new PrivateCacheError("Private cache records must not contain cycles.", "cyclic_record");
    }
    seen.add(value);
    let total = 0;
    if (Array.isArray(value)) {
      for (const item of value) total += logicalByteSize(scope, item, seen);
    } else {
      for (const [key, item] of Object.entries(value)) {
        if (key === "byteSize") continue;
        total += logicalByteSize(scope, key, seen);
        total += logicalByteSize(scope, item, seen, key);
      }
    }
    seen.delete(value);
    return total;
  }

  function isQuotaExceeded(error) {
    return Boolean(
      error
      && (error.name === "QuotaExceededError" || error.code === "quota_exceeded"),
    );
  }

  function primaryKeyFor(category, record) {
    if (category === "messages") {
      if (!record.conversationId || !record.messageId) {
        throw new PrivateCacheError(
          "Message records require conversationId and messageId.",
          "invalid_record_key",
        );
      }
      return [record.conversationId, record.messageId];
    }
    const keyName = {
      conversations: "conversationId",
      segments: "segmentKey",
      searchSnapshots: "snapshotKey",
      thumbnails: "mediaKey",
      mediaObjects: "mediaKey",
    }[category];
    const key = record[keyName];
    if (!key) {
      throw new PrivateCacheError(
        `${category} records require ${keyName}.`,
        "invalid_record_key",
      );
    }
    return key;
  }

  function keyToken(category, key) {
    return `${category}:${Array.isArray(key) ? key.join("\u0000") : String(key)}`;
  }

  function validCategory(category) {
    if (!DATA_STORES.includes(category)) {
      throw new PrivateCacheError(`Unsupported private cache category: ${category}`, "invalid_category");
    }
  }

  function requiredIdentifier(value, label) {
    const normalized = String(value ?? "").trim();
    if (!normalized || normalized.length > 2048) {
      throw new PrivateCacheError(
        `${label} is required and must be a bounded string.`,
        "invalid_message_window",
      );
    }
    return normalized;
  }

  function boundaryValue(value) {
    if (value === true || value === false) return value;
    return "unknown";
  }

  function finitePosition(value) {
    const position = Number(value);
    return Number.isInteger(position) && position > 0 ? position : null;
  }

  function segmentKeyFor({
    conversationId,
    direction,
    firstMessageId,
    lastMessageId,
    anchorMessageId,
  }) {
    return JSON.stringify([
      conversationId,
      direction,
      firstMessageId,
      lastMessageId,
      anchorMessageId || "",
    ]);
  }

  function normalizedSearchText(value) {
    return String(value ?? "")
      .normalize("NFKC")
      .trim()
      .replace(/\s+/gu, " ")
      .toLocaleLowerCase();
  }

  function parsedMessageTime(value) {
    const timestamp = Date.parse(String(value || ""));
    return Number.isFinite(timestamp) ? timestamp : 0;
  }

  function abortError(scope, message = "Cached search was cancelled.") {
    if (scope.DOMException) return new scope.DOMException(message, "AbortError");
    const error = new Error(message);
    error.name = "AbortError";
    return error;
  }

  function searchTextWithOffsets(value) {
    const original = String(value ?? "");
    let normalized = "";
    const offsets = [];
    let previousWasSpace = false;
    let originalOffset = 0;
    for (const character of original) {
      const isSpace = /\s/u.test(character);
      const replacement = isSpace
        ? " "
        : character.normalize("NFKC").toLocaleLowerCase();
      if (isSpace && previousWasSpace) {
        originalOffset += character.length;
        continue;
      }
      for (let index = 0; index < replacement.length; index += 1) {
        normalized += replacement[index];
        offsets.push(originalOffset);
      }
      previousWasSpace = isSpace;
      originalOffset += character.length;
    }
    return { original, normalized, offsets };
  }

  function normalizedMatchRanges(value, normalizedQuery) {
    const searchable = searchTextWithOffsets(value);
    const ranges = [];
    if (!normalizedQuery) return { ...searchable, ranges };
    let cursor = 0;
    let index = searchable.normalized.indexOf(normalizedQuery, cursor);
    while (index !== -1) {
      const start = searchable.offsets[index] ?? 0;
      const lastOffset = searchable.offsets[index + normalizedQuery.length - 1] ?? start;
      const lastCharacter = searchable.original.slice(lastOffset).match(/^./u)?.[0] || "";
      ranges.push({ start, end: lastOffset + lastCharacter.length });
      cursor = index + Math.max(1, normalizedQuery.length);
      index = searchable.normalized.indexOf(normalizedQuery, cursor);
    }
    return { ...searchable, ranges };
  }

  function searchSnippet(value, ranges = []) {
    const original = String(value ?? "");
    const firstRange = ranges[0] || null;
    const firstStart = Number(firstRange?.start) || 0;
    const firstEnd = Number(firstRange?.end) || Math.min(original.length, 180);
    const start = Math.max(0, firstStart - 64);
    const end = Math.min(
      original.length,
      Math.max(firstEnd + 128, start + 180),
    );
    const prefix = start > 0 ? "…" : "";
    const suffix = end < original.length ? "…" : "";
    return {
      text: `${prefix}${original.slice(start, end)}${suffix}`,
      sourceStart: start,
      matchRanges: ranges
        .filter((range) => range.end > start && range.start < end)
        .map((range) => ({
          start: prefix.length + Math.max(start, range.start) - start,
          end: prefix.length + Math.min(end, range.end) - start,
        })),
    };
  }

  function createPrivateCache(options = {}) {
    const scope = options.scope || globalThis;
    const indexedDb = options.indexedDB || scope.indexedDB;
    const storageManager = options.storageManager || scope.navigator?.storage || null;
    const now = typeof options.now === "function" ? options.now : () => Date.now();
    const yieldControl = typeof options.yieldControl === "function"
      ? options.yieldControl
      : () => new Promise((resolve) => scope.setTimeout(resolve, 0));
    const dbName = String(options.dbName || DEFAULT_DB_NAME);
    const dbVersion = Number(options.dbVersion || DB_VERSION);
    const ownerId = String(options.ownerId || stableOwnerId(scope));
    const leaseMs = Math.max(1000, Number(options.leaseMs) || DEFAULT_LEASE_MS);
    const migrationHook = typeof options.migrationHook === "function"
      ? options.migrationHook
      : null;
    const writeAttemptHook = typeof options.writeAttemptHook === "function"
      ? options.writeAttemptHook
      : null;
    const messageWindowMutationHook = typeof options.messageWindowMutationHook === "function"
      ? options.messageWindowMutationHook
      : null;
    const resumeMigrationHook = typeof options.resumeMigrationHook === "function"
      ? options.resumeMigrationHook
      : null;
    let connection = null;
    let opening = null;
    let resumeConnection = null;
    let resumeOpening = null;
    let mutationQueue = Promise.resolve();

    if (!indexedDb) {
      throw new PrivateCacheError("IndexedDB is unavailable.", "indexeddb_unavailable");
    }

    function createOrValidateStores(database, transaction) {
      for (const [name, definition] of Object.entries(STORE_DEFINITIONS)) {
        let store;
        if (!database.objectStoreNames.contains(name)) {
          store = database.createObjectStore(name, { keyPath: definition.keyPath });
        } else {
          store = transaction.objectStore(name);
          if (!keyPathMatches(store.keyPath, definition.keyPath)) {
            throw new PrivateCacheError(
              `Incompatible keyPath for ${name}.`,
              "incompatible_schema",
            );
          }
        }
        for (const [indexName, keyPath] of definition.indexes) {
          if (!store.indexNames.contains(indexName)) {
            store.createIndex(indexName, keyPath, { unique: false });
          }
        }
      }
    }

    async function validateSchema(database) {
      for (const [name, definition] of Object.entries(STORE_DEFINITIONS)) {
        if (!database.objectStoreNames.contains(name)) {
          throw new PrivateCacheError(`Missing object store: ${name}`, "incompatible_schema");
        }
        const transaction = database.transaction(name, "readonly");
        const store = transaction.objectStore(name);
        if (!keyPathMatches(store.keyPath, definition.keyPath)) {
          transaction.abort();
          throw new PrivateCacheError(
            `Incompatible keyPath for ${name}.`,
            "incompatible_schema",
          );
        }
        for (const [indexName] of definition.indexes) {
          if (!store.indexNames.contains(indexName)) {
            transaction.abort();
            throw new PrivateCacheError(
              `Missing index ${name}.${indexName}.`,
              "incompatible_schema",
            );
          }
        }
        await transactionResult(transaction);
      }
    }

    async function finalizeOpen(database, upgraded) {
      await validateSchema(database);
      const at = now();
      const transaction = database.transaction(META_STORE, "readwrite");
      const store = transaction.objectStore(META_STORE);
      const existingStats = await requestResult(store.get("stats"));
      const existingClock = await requestResult(store.get("clock"));
      const existingGeneration = await requestResult(store.get("cacheGeneration"));
      store.put(normalizedStats(existingStats, at));
      if (!existingGeneration?.value) {
        store.put({
          key: "cacheGeneration",
          value: `cache-${stableOwnerId(scope)}`,
          updatedAt: at,
        });
      }
      store.put({
        key: "schema",
        dbVersion,
        contentSchemaVersion: CONTENT_SCHEMA_VERSION,
        updatedAt: at,
      });
      store.put({
        key: "migration",
        state: "idle",
        fromVersion: upgraded?.oldVersion ?? dbVersion,
        toVersion: dbVersion,
        updatedAt: at,
      });
      store.put({
        key: "clock",
        lastObservedWallClock: Math.max(
          Number(existingClock?.lastObservedWallClock) || 0,
          at,
        ),
        rollbackDetected: Boolean(
          existingClock?.rollbackDetected
          || (
            Number(existingClock?.lastObservedWallClock)
            && at + CLOCK_ROLLBACK_TOLERANCE_MS
              < Number(existingClock.lastObservedWallClock)
          )
        ),
        observedAt: at,
      });
      await transactionResult(transaction);
      return database;
    }

    function open() {
      if (connection) return Promise.resolve(connection);
      if (opening) return opening;
      let upgraded = null;
      opening = new Promise((resolve, reject) => {
        const request = indexedDb.open(dbName, dbVersion);
        request.addEventListener("upgradeneeded", (event) => {
          const database = request.result;
          const transaction = request.transaction;
          try {
            createOrValidateStores(database, transaction);
            upgraded = { oldVersion: event.oldVersion, newVersion: event.newVersion };
            const meta = transaction.objectStore(META_STORE);
            const at = now();
            meta.put({
              key: "migration",
              state: "running",
              fromVersion: event.oldVersion,
              toVersion: event.newVersion,
              updatedAt: at,
            });
            meta.put({
              key: "schema",
              dbVersion,
              contentSchemaVersion: CONTENT_SCHEMA_VERSION,
              updatedAt: at,
            });
            meta.put(emptyStats(at));
            migrationHook?.({
              database,
              transaction,
              oldVersion: event.oldVersion,
              newVersion: event.newVersion,
            });
          } catch (error) {
            transaction.abort();
            reject(error);
          }
        });
        request.addEventListener("blocked", () => {
          reject(new PrivateCacheError(
            "Private cache schema upgrade is blocked by another page.",
            "schema_upgrade_blocked",
          ));
        }, { once: true });
        request.addEventListener("error", () => {
          reject(request.error || new PrivateCacheError(
            "Failed to open private cache.",
            "open_failed",
          ));
        }, { once: true });
        request.addEventListener("success", async () => {
          const database = request.result;
          database.addEventListener("versionchange", () => {
            database.close();
            if (connection === database) connection = null;
          });
          try {
            connection = await finalizeOpen(database, upgraded);
            resolve(connection);
          } catch (error) {
            database.close();
            connection = null;
            reject(error);
          }
        }, { once: true });
      }).finally(() => {
        opening = null;
      });
      return opening;
    }

    function openResumeDatabase() {
      if (resumeConnection) return Promise.resolve(resumeConnection);
      if (resumeOpening) return resumeOpening;
      let initialCreation = false;
      resumeOpening = new Promise((resolve, reject) => {
        const request = indexedDb.open(RESUME_DB_NAME, RESUME_DB_VERSION);
        request.addEventListener("upgradeneeded", (event) => {
          const database = request.result;
          const transaction = request.transaction;
          initialCreation = event.oldVersion === 0;
          try {
            let store;
            if (!database.objectStoreNames.contains(RESUME_STORE)) {
              store = database.createObjectStore(RESUME_STORE, { keyPath: "key" });
            } else {
              store = transaction.objectStore(RESUME_STORE);
              if (store.keyPath !== "key") {
                throw new PrivateCacheError(
                  "Incompatible mobile resume state schema.",
                  "incompatible_resume_schema",
                );
              }
            }
            resumeMigrationHook?.({
              database,
              transaction,
              oldVersion: event.oldVersion,
              newVersion: event.newVersion,
            });
          } catch (error) {
            transaction.abort();
            reject(error);
          }
        });
        request.addEventListener("blocked", () => {
          reject(new PrivateCacheError(
            "Mobile resume state schema upgrade is blocked by another page.",
            "resume_schema_upgrade_blocked",
          ));
        }, { once: true });
        request.addEventListener("error", async () => {
          if (initialCreation) {
            await deleteDatabase(indexedDb, RESUME_DB_NAME).catch(() => {});
          }
          reject(request.error || new PrivateCacheError(
            "Failed to open mobile resume state.",
            "resume_open_failed",
          ));
        }, { once: true });
        request.addEventListener("success", () => {
          const database = request.result;
          if (
            !database.objectStoreNames.contains(RESUME_STORE)
            || database.transaction(RESUME_STORE, "readonly").objectStore(RESUME_STORE).keyPath
              !== "key"
          ) {
            database.close();
            reject(new PrivateCacheError(
              "Incompatible mobile resume state schema.",
              "incompatible_resume_schema",
            ));
            return;
          }
          database.addEventListener("versionchange", () => {
            database.close();
            if (resumeConnection === database) resumeConnection = null;
          });
          resumeConnection = database;
          resolve(database);
        }, { once: true });
      }).finally(() => {
        resumeOpening = null;
      });
      return resumeOpening;
    }

    function close() {
      connection?.close();
      resumeConnection?.close();
      connection = null;
      resumeConnection = null;
      opening = null;
      resumeOpening = null;
    }

    async function observeClock() {
      return withLease(async (token) => {
        const database = await open();
        const current = now();
        const transaction = database.transaction(META_STORE, "readwrite");
        const store = transaction.objectStore(META_STORE);
        await assertAndRenewLease(store, token);
        const previous = await requestResult(store.get("clock"));
        const lastObservedWallClock = Number(previous?.lastObservedWallClock) || 0;
        const rollbackDetected = Boolean(
          lastObservedWallClock
          && current + CLOCK_ROLLBACK_TOLERANCE_MS < lastObservedWallClock,
        );
        const next = {
          key: "clock",
          lastObservedWallClock: Math.max(lastObservedWallClock, current),
          rollbackDetected: Boolean(previous?.rollbackDetected || rollbackDetected),
          observedAt: current,
        };
        store.put(next);
        await transactionResult(transaction);
        return next;
      });
    }

    async function getMeta(key) {
      const database = await open();
      const transaction = database.transaction(META_STORE, "readonly");
      const value = await requestResult(transaction.objectStore(META_STORE).get(key));
      await transactionResult(transaction);
      return value || null;
    }

    async function getStats() {
      return normalizedStats(await getMeta("stats"), now());
    }

    async function getCacheGeneration() {
      const record = await getMeta("cacheGeneration");
      return String(record?.value || "");
    }

    function searchSnapshotKey(query, sortMode = "relevance") {
      const normalizedQuery = normalizedSearchText(query);
      const normalizedSort = sortMode === "latest" ? "latest" : "relevance";
      return JSON.stringify([SEARCH_SNAPSHOT_VERSION, normalizedQuery, normalizedSort]);
    }

    async function readMessageSearchBatch(afterKey = null, limit = DEFAULT_SEARCH_BATCH_SIZE) {
      const database = await open();
      const transaction = database.transaction("messages", "readonly");
      const store = transaction.objectStore("messages");
      const range = afterKey === null
        ? null
        : scope.IDBKeyRange.lowerBound(afterKey, true);
      const records = [];
      let lastKey = afterKey;
      await new Promise((resolve, reject) => {
        const request = store.openCursor(range);
        request.addEventListener("error", () => reject(request.error), { once: true });
        request.addEventListener("success", () => {
          const cursor = request.result;
          if (!cursor || records.length >= limit) {
            resolve();
            return;
          }
          records.push(cursor.value);
          lastKey = cursor.primaryKey;
          cursor.continue();
        });
      });
      await transactionResult(transaction);
      return { records, lastKey };
    }

    async function cachedSearchSources() {
      const database = await open();
      const transaction = database.transaction(
        ["conversations", "segments"],
        "readonly",
      );
      const [conversations, segments] = await Promise.all([
        requestResult(transaction.objectStore("conversations").getAll()),
        requestResult(transaction.objectStore("segments").getAll()),
      ]);
      await transactionResult(transaction);
      const at = now();
      const conversationTitles = new Map(
        conversations
          .filter((record) => (
            record?.conversationId
            && record.contentVersion === 1
            && Number(record.expiresAt) > at
          ))
          .map((record) => [record.conversationId, String(record.title || "")]),
      );
      const segmentStates = new Map();
      const segmentsByMessage = new Map();
      for (const segment of segments) {
        const messageIds = Array.isArray(segment?.messageIds)
          ? segment.messageIds.map((value) => String(value || "").trim()).filter(Boolean)
          : [];
        const uniqueMessageIds = new Set(messageIds);
        if (
          !segment?.segmentKey
          || !segment.conversationId
          || segment.contentVersion !== 1
          || segment.partial === true
          || Number(segment.expiresAt) <= at
          || !messageIds.length
          || uniqueMessageIds.size !== messageIds.length
          || (Array.isArray(segment.missingMessageIds) && segment.missingMessageIds.length)
          || (
            Number(segment.expectedMessageCount) > 0
            && Number(segment.expectedMessageCount) !== messageIds.length
          )
        ) continue;
        const state = {
          segment,
          expectedMessageIds: uniqueMessageIds,
          seenMessageIds: new Set(),
          valid: true,
        };
        segmentStates.set(segment.segmentKey, state);
        for (const messageId of uniqueMessageIds) {
          const token = keyToken("messages", [segment.conversationId, messageId]);
          if (!segmentsByMessage.has(token)) segmentsByMessage.set(token, new Set());
          segmentsByMessage.get(token).add(segment.segmentKey);
        }
      }
      return { conversationTitles, segmentStates, segmentsByMessage };
    }

    function relevanceComparator(left, right) {
      return (
        Number(right.relevanceScore) - Number(left.relevanceScore)
        || Number(right.validatedAt) - Number(left.validatedAt)
        || parsedMessageTime(right.messageTime) - parsedMessageTime(left.messageTime)
        || String(left.messageId).localeCompare(String(right.messageId))
        || String(left.conversationId).localeCompare(String(right.conversationId))
      );
    }

    function latestComparator(left, right) {
      return (
        parsedMessageTime(right.messageTime) - parsedMessageTime(left.messageTime)
        || String(left.messageId).localeCompare(String(right.messageId))
        || String(left.conversationId).localeCompare(String(right.conversationId))
      );
    }

    async function searchCachedMessages({
      query,
      sortMode = "relevance",
      offset = 0,
      limit = 20,
      signal = null,
      batchSize = DEFAULT_SEARCH_BATCH_SIZE,
    } = {}) {
      const originalQuery = String(query ?? "").trim();
      const normalizedQuery = normalizedSearchText(originalQuery);
      const visibleCharacters = Array.from(normalizedQuery)
        .filter((character) => !/\s/u.test(character)).length;
      if (visibleCharacters < 2) {
        return {
          sourceMode: "cached-only",
          query: originalQuery,
          normalizedQuery,
          sortMode: sortMode === "latest" ? "latest" : "relevance",
          items: [],
          resultRefs: [],
          totalConversations: 0,
          totalMessages: 0,
          hasMore: false,
          nextOffset: 0,
          coverage: {
            conversationCount: 0,
            messageCount: 0,
            lastUpdatedAt: 0,
          },
          performance: {
            durationMs: 0,
            maxBatchMs: 0,
            scannedMessages: 0,
          },
        };
      }
      const startedAt = scope.performance?.now?.() ?? now();
      const normalizedSort = sortMode === "latest" ? "latest" : "relevance";
      const safeOffset = Math.max(0, Number(offset) || 0);
      const safeLimit = Math.max(1, Math.min(500, Number(limit) || 20));
      const safeBatchSize = Math.max(10, Math.min(500, Number(batchSize) || DEFAULT_SEARCH_BATCH_SIZE));
      const {
        conversationTitles,
        segmentStates,
        segmentsByMessage,
      } = await cachedSearchSources();
      const candidates = [];
      let afterKey = null;
      let scannedMessages = 0;
      let maxBatchMs = 0;
      while (true) {
        if (signal?.aborted) throw abortError(scope);
        const batch = await readMessageSearchBatch(afterKey, safeBatchSize);
        if (!batch.records.length) break;
        const batchStartedAt = scope.performance?.now?.() ?? now();
        for (const message of batch.records) {
          if (signal?.aborted) throw abortError(scope);
          scannedMessages += 1;
          const token = keyToken(
            "messages",
            [message?.conversationId || "", message?.messageId || ""],
          );
          const segmentKeys = segmentsByMessage.get(token);
          if (!segmentKeys?.size) continue;
          const structurallyValid = Boolean(
            message?.conversationId
            && message?.messageId
            && message.contentVersion === 1
            && Number(message.expiresAt) > now()
            && typeof message.text === "string"
            && normalizedSearchText(message.text),
          );
          if (!structurallyValid) {
            segmentKeys.forEach((segmentKey) => {
              const state = segmentStates.get(segmentKey);
              if (state) state.valid = false;
            });
            continue;
          }
          segmentKeys.forEach((segmentKey) => {
            segmentStates.get(segmentKey)?.seenMessageIds.add(message.messageId);
          });
          const conversationTitle = conversationTitles.get(message.conversationId) || "";
          const textMatch = normalizedMatchRanges(message.text, normalizedQuery);
          const titleMatch = normalizedMatchRanges(conversationTitle, normalizedQuery);
          if (!textMatch.ranges.length && !titleMatch.ranges.length) continue;
          const firstRange = textMatch.ranges[0] || null;
          const snippet = searchSnippet(message.text, textMatch.ranges);
          const matchCount = textMatch.ranges.length + titleMatch.ranges.length;
          const firstPosition = firstRange?.start ?? Number.MAX_SAFE_INTEGER;
          candidates.push({
            conversationId: message.conversationId,
            conversationTitle: conversationTitle || "未命名会话",
            messageId: message.messageId,
            messageTime: String(message.messageTime || ""),
            role: String(message.role || ""),
            snippet: snippet.text,
            matchRanges: snippet.matchRanges,
            cachedAt: Number(message.cachedAt) || 0,
            validatedAt: Number(message.validatedAt) || 0,
            sourceMode: "cached-only",
            titleMatched: titleMatch.ranges.length > 0,
            matchCount,
            firstMatchPosition: Number.isFinite(firstPosition) ? firstPosition : null,
            relevanceScore: (
              100_000
              + (titleMatch.ranges.length ? 10_000 : 0)
              + Math.min(100, matchCount) * 100
              - Math.min(99, Number.isFinite(firstPosition) ? firstPosition : 99)
            ),
            segmentKeys: [...segmentKeys],
          });
        }
        const batchFinishedAt = scope.performance?.now?.() ?? now();
        maxBatchMs = Math.max(maxBatchMs, batchFinishedAt - batchStartedAt);
        afterKey = batch.lastKey;
        await yieldControl();
      }
      const completeSegmentKeys = new Set(
        [...segmentStates.entries()]
          .filter(([, state]) => (
            state.valid
            && state.seenMessageIds.size === state.expectedMessageIds.size
          ))
          .map(([segmentKey]) => segmentKey),
      );
      if (completeSegmentKeys.size) {
        const database = await open();
        const transaction = database.transaction("segments", "readonly");
        const currentSegments = await requestResult(
          transaction.objectStore("segments").getAll(),
        );
        await transactionResult(transaction);
        const at = now();
        const currentKeys = new Set(
          currentSegments
            .filter((segment) => (
              completeSegmentKeys.has(segment?.segmentKey)
              && segment.contentVersion === 1
              && segment.partial !== true
              && Number(segment.expiresAt) > at
              && Array.isArray(segment.messageIds)
              && segment.messageIds.length > 0
              && new Set(segment.messageIds).size === segment.messageIds.length
              && (
                Number(segment.expectedMessageCount) <= 0
                || Number(segment.expectedMessageCount) === segment.messageIds.length
              )
            ))
            .map((segment) => segment.segmentKey),
        );
        for (const segmentKey of completeSegmentKeys) {
          if (!currentKeys.has(segmentKey)) completeSegmentKeys.delete(segmentKey);
        }
      }
      const completeMessageTokens = new Set();
      let lastUpdatedAt = 0;
      for (const segmentKey of completeSegmentKeys) {
        const segment = segmentStates.get(segmentKey).segment;
        lastUpdatedAt = Math.max(
          lastUpdatedAt,
          Number(segment.validatedAt) || Number(segment.cachedAt) || 0,
        );
        for (const messageId of segment.messageIds) {
          completeMessageTokens.add(
            keyToken("messages", [segment.conversationId, messageId]),
          );
        }
      }
      const deduped = new Map();
      for (const candidate of candidates) {
        const validSegmentKeys = candidate.segmentKeys.filter(
          (segmentKey) => completeSegmentKeys.has(segmentKey),
        );
        if (!validSegmentKeys.length) continue;
        const candidateToken = keyToken(
          "messages",
          [candidate.conversationId, candidate.messageId],
        );
        const segmentRecords = validSegmentKeys.map(
          (segmentKey) => segmentStates.get(segmentKey).segment,
        );
        const next = {
          ...candidate,
          hasCompletePreview: segmentRecords.some(
            (segment) => segment.direction === "preview",
          ),
          hasOpenableSegment: true,
        };
        delete next.segmentKeys;
        if (!deduped.has(candidateToken)) deduped.set(candidateToken, next);
      }
      const comparator = normalizedSort === "latest" ? latestComparator : relevanceComparator;
      const grouped = new Map();
      [...deduped.values()].sort(comparator).forEach((hit) => {
        if (!grouped.has(hit.conversationId)) grouped.set(hit.conversationId, []);
        grouped.get(hit.conversationId).push(hit);
      });
      const groups = [...grouped.entries()].map(([conversationId, hits]) => {
        const orderedHits = [...hits].sort(comparator);
        const bestHit = orderedHits[0];
        const asApiHit = (hit) => ({
          conversation_id: hit.conversationId,
          message_id: hit.messageId,
          role: hit.role,
          snippet: hit.snippet,
          create_time_iso: hit.messageTime,
          match_ranges: hit.matchRanges,
          cached_at: hit.cachedAt,
          validated_at: hit.validatedAt,
          source_mode: "cached-only",
          match_type: "cached-only",
          complete_preview: hit.hasCompletePreview,
          openable_segment: hit.hasOpenableSegment,
          relevance_score: hit.relevanceScore,
        });
        return {
          conversation_id: conversationId,
          title: bestHit.conversationTitle,
          best_hit: asApiHit(bestHit),
          other_hits: orderedHits.slice(1).map(asApiHit),
          match_count: orderedHits.length,
          other_match_count: Math.max(0, orderedHits.length - 1),
          source_mode: "cached-only",
          sort_value: bestHit,
        };
      }).sort((left, right) => comparator(left.sort_value, right.sort_value));
      const resultRefs = groups.flatMap((group) => (
        [group.best_hit, ...group.other_hits].map((hit) => ({
          conversationId: hit.conversation_id,
          messageId: hit.message_id,
        }))
      ));
      const items = groups
        .slice(safeOffset, safeOffset + safeLimit)
        .map((group) => {
          const clean = { ...group };
          delete clean.sort_value;
          return clean;
        });
      const finishedAt = scope.performance?.now?.() ?? now();
      return {
        sourceMode: "cached-only",
        query: originalQuery,
        normalizedQuery,
        sortMode: normalizedSort,
        items,
        resultRefs,
        totalConversations: groups.length,
        totalMessages: deduped.size,
        hasMore: safeOffset + items.length < groups.length,
        nextOffset: safeOffset + items.length,
        coverage: {
          conversationCount: new Set(
            [...completeSegmentKeys].map(
              (segmentKey) => segmentStates.get(segmentKey).segment.conversationId,
            ),
          ).size,
          messageCount: completeMessageTokens.size,
          lastUpdatedAt,
        },
        performance: {
          durationMs: Math.max(0, finishedAt - startedAt),
          maxBatchMs: Math.max(0, maxBatchMs),
          scannedMessages,
        },
      };
    }

    async function saveSearchSnapshot({
      query,
      sortMode = "relevance",
      resultRefs = [],
      cacheGeneration = "",
    } = {}) {
      const normalizedQuery = normalizedSearchText(query);
      if (!normalizedQuery) {
        throw new PrivateCacheError("Search snapshots require a query.", "invalid_search_snapshot");
      }
      const seen = new Set();
      const normalizedRefs = [];
      for (const ref of Array.isArray(resultRefs) ? resultRefs : []) {
        const conversationId = String(ref?.conversationId || "").trim();
        const messageId = String(ref?.messageId || "").trim();
        if (!conversationId || !messageId) continue;
        const token = keyToken("messages", [conversationId, messageId]);
        if (seen.has(token)) continue;
        seen.add(token);
        normalizedRefs.push({ conversationId, messageId });
      }
      const at = now();
      const normalizedSort = sortMode === "latest" ? "latest" : "relevance";
      const generation = String(cacheGeneration || await getCacheGeneration());
      return putRecord("searchSnapshots", {
        snapshotKey: searchSnapshotKey(normalizedQuery, normalizedSort),
        snapshotVersion: SEARCH_SNAPSHOT_VERSION,
        sourceMode: "cached-only",
        query: String(query || "").trim(),
        normalizedQuery,
        sortMode: normalizedSort,
        resultRefs: normalizedRefs,
        cacheGeneration: generation,
        cachedAt: at,
        validatedAt: at,
        lastAccessedAt: at,
        expiresAt: at + RESUME_TTL_MS,
      });
    }

    async function getSearchSnapshot({ query, sortMode = "relevance" } = {}) {
      const normalizedSort = sortMode === "latest" ? "latest" : "relevance";
      const record = await getRecord(
        "searchSnapshots",
        searchSnapshotKey(query, normalizedSort),
      );
      if (
        !record
        || record.snapshotVersion !== SEARCH_SNAPSHOT_VERSION
        || record.sourceMode !== "cached-only"
        || record.normalizedQuery !== normalizedSearchText(query)
        || record.sortMode !== normalizedSort
        || !Array.isArray(record.resultRefs)
        || record.cacheGeneration !== await getCacheGeneration()
      ) return null;
      return record;
    }

    async function activeReaderProtectedTokens() {
      const database = await open();
      const transaction = database.transaction(META_STORE, "readonly");
      const records = await requestResult(transaction.objectStore(META_STORE).getAll());
      await transactionResult(transaction);
      const at = now();
      const protectedTokens = new Set();
      for (const record of records) {
        if (
          !String(record?.key || "").startsWith("activeReader:")
          || Number(record.expiresAt) <= at
        ) continue;
        for (const token of record.protectedTokens || []) {
          if (typeof token === "string" && token) protectedTokens.add(token);
        }
      }
      return protectedTokens;
    }

    async function acquireLease() {
      const database = await open();
      const at = now();
      const leaseId = `${ownerId}:${at}:${Math.random().toString(16).slice(2)}`;
      const transaction = database.transaction(META_STORE, "readwrite");
      const store = transaction.objectStore(META_STORE);
      const current = await requestResult(store.get("writerLease"));
      if (
        current
        && current.ownerId !== ownerId
        && Number(current.expiresAt) > at
      ) {
        transaction.abort();
        try {
          await transactionResult(transaction);
        } catch {
          // The explicit LeaseBusyError below is the public result.
        }
        throw new LeaseBusyError();
      }
      const token = {
        key: "writerLease",
        ownerId,
        leaseId,
        acquiredAt: at,
        renewedAt: at,
        expiresAt: at + leaseMs,
      };
      store.put(token);
      await transactionResult(transaction);
      return token;
    }

    async function assertAndRenewLease(metaStore, token) {
      const at = now();
      const lease = await requestResult(metaStore.get("writerLease"));
      if (
        !lease
        || lease.ownerId !== token.ownerId
        || lease.leaseId !== token.leaseId
        || Number(lease.expiresAt) <= at
      ) {
        throw new LeaseBusyError("Private cache writer lease expired or changed.");
      }
      const renewed = {
        ...lease,
        renewedAt: at,
        expiresAt: at + leaseMs,
      };
      metaStore.put(renewed);
      token.renewedAt = renewed.renewedAt;
      token.expiresAt = renewed.expiresAt;
    }

    async function releaseLease(token) {
      const database = connection;
      if (!database || !token) return;
      const transaction = database.transaction(META_STORE, "readwrite");
      const store = transaction.objectStore(META_STORE);
      const lease = await requestResult(store.get("writerLease"));
      if (lease?.ownerId === token.ownerId && lease?.leaseId === token.leaseId) {
        store.delete("writerLease");
      }
      await transactionResult(transaction);
    }

    async function withLease(callback) {
      const execute = async () => {
        const token = await acquireLease();
        try {
          return await callback(token);
        } finally {
          await releaseLease(token).catch(() => {});
        }
      };
      const result = mutationQueue.then(execute, execute);
      mutationQueue = result.catch(() => {});
      return result;
    }

    function prepareRecord(category, input) {
      validCategory(category);
      if (!input || typeof input !== "object" || Array.isArray(input)) {
        throw new PrivateCacheError("Private cache records must be objects.", "invalid_record");
      }
      const at = now();
      const record = cloneForWrite(scope, input);
      primaryKeyFor(category, record);
      record.cachedAt = Number(record.cachedAt) || at;
      record.validatedAt = Number(record.validatedAt) || at;
      record.expiresAt = Number(record.expiresAt) || (record.validatedAt + TTL_MS);
      record.lastAccessedAt = Number(record.lastAccessedAt) || at;
      record.byteSize = logicalByteSize(scope, record);
      return record;
    }

    async function putRecordOnce(category, record, token, attempt) {
      writeAttemptHook?.({ category, record, attempt });
      const database = await open();
      const key = primaryKeyFor(category, record);
      const transaction = database.transaction([META_STORE, category], "readwrite");
      const meta = transaction.objectStore(META_STORE);
      const store = transaction.objectStore(category);
      try {
        await assertAndRenewLease(meta, token);
        const existing = await requestResult(store.get(key));
        const stats = normalizedStats(await requestResult(meta.get("stats")), now());
        store.put(record);
        const oldBytes = Math.max(0, Number(existing?.byteSize) || 0);
        const delta = record.byteSize - oldBytes;
        stats.totalBytes = Math.max(0, stats.totalBytes + delta);
        stats.categoryBytes[category] = Math.max(
          0,
          stats.categoryBytes[category] + delta,
        );
        if (!existing) {
          stats.categoryCounts[category] += 1;
        }
        stats.updatedAt = now();
        meta.put(stats);
        await transactionResult(transaction);
        return cloneForWrite(scope, record);
      } catch (error) {
        try {
          transaction.abort();
        } catch {
          // The transaction may already have completed its abort.
        }
        try {
          await transactionResult(transaction);
        } catch {
          // Preserve the original failure.
        }
        throw error;
      }
    }

    async function estimateCapacity({ requestPersistence = false } = {}) {
      let usage = null;
      let quota = null;
      let persisted = null;
      let persistenceRequested = false;
      let estimateAvailable = false;
      if (storageManager?.estimate) {
        try {
          const estimate = await storageManager.estimate();
          usage = Number.isFinite(Number(estimate?.usage)) ? Number(estimate.usage) : null;
          quota = Number.isFinite(Number(estimate?.quota)) ? Number(estimate.quota) : null;
          estimateAvailable = quota !== null && quota > 0;
        } catch {
          estimateAvailable = false;
        }
      }
      if (storageManager?.persisted) {
        try {
          persisted = Boolean(await storageManager.persisted());
        } catch {
          persisted = null;
        }
      }
      if (requestPersistence && !persisted && storageManager?.persist) {
        persistenceRequested = true;
        try {
          persisted = Boolean(await storageManager.persist());
        } catch {
          persisted = false;
        }
      }
      const effectiveTarget = estimateAvailable
        ? Math.min(PRODUCT_TARGET_BYTES, Math.floor(quota * 0.5))
        : DEFAULT_TARGET_BYTES;
      const effectiveCeiling = estimateAvailable
        ? Math.min(PRODUCT_CEILING_BYTES, Math.floor(quota * 0.7))
        : DEFAULT_CEILING_BYTES;
      const result = {
        usage,
        quota,
        estimateAvailable,
        persisted,
        persistenceRequested,
        effectiveTarget,
        effectiveCeiling,
        productTarget: PRODUCT_TARGET_BYTES,
        productCeiling: PRODUCT_CEILING_BYTES,
        estimatedAt: now(),
      };
      return result;
    }

    async function requestPersistence() {
      return withLease(async (token) => {
        const result = await estimateCapacity({ requestPersistence: true });
        await recordCapacity(result, token);
        return result;
      });
    }

    async function readRecord(category, key, { allowExpired = false, touch = false } = {}, token = null) {
      validCategory(category);
      const database = await open();
      const transaction = database.transaction(
        [category, ...(touch ? [META_STORE] : [])],
        touch ? "readwrite" : "readonly",
      );
      const store = transaction.objectStore(category);
      if (touch) {
        await assertAndRenewLease(transaction.objectStore(META_STORE), token);
      }
      const value = await requestResult(store.get(key));
      if (!value) {
        await transactionResult(transaction);
        return null;
      }
      const expired = Number(value.expiresAt) <= now();
      if (touch && (!expired || allowExpired)) {
        value.lastAccessedAt = now();
        store.put(value);
      }
      await transactionResult(transaction);
      if (expired && !allowExpired) return null;
      return { ...value, expired };
    }

    async function getRecord(category, key, optionsForRead = {}) {
      if (optionsForRead.touch) {
        return withLease((token) => readRecord(category, key, optionsForRead, token));
      }
      return readRecord(category, key, optionsForRead);
    }

    async function collectBatch(
      category,
      {
        indexName = "by_last_accessed_at",
        range = null,
        limit = DEFAULT_BATCH_SIZE,
        predicate = null,
      } = {},
    ) {
      const database = await open();
      const transaction = database.transaction(category, "readonly");
      const store = transaction.objectStore(category);
      const source = indexName ? store.index(indexName) : store;
      const entries = [];
      await new Promise((resolve, reject) => {
        const request = source.openCursor(range);
        request.addEventListener("error", () => reject(request.error), { once: true });
        request.addEventListener("success", () => {
          const cursor = request.result;
          if (!cursor || entries.length >= limit) {
            resolve();
            return;
          }
          if (!predicate || predicate(cursor.value)) {
            entries.push({ key: cursor.primaryKey, value: cursor.value });
          }
          cursor.continue();
        });
      });
      await transactionResult(transaction);
      return entries;
    }

    async function deleteBatch(category, entries, token) {
      if (!entries.length) return { deletedRecords: 0, freedBytes: 0 };
      const database = await open();
      const transaction = database.transaction([META_STORE, category], "readwrite");
      const meta = transaction.objectStore(META_STORE);
      const store = transaction.objectStore(category);
      await assertAndRenewLease(meta, token);
      const stats = normalizedStats(await requestResult(meta.get("stats")), now());
      let deletedRecords = 0;
      let freedBytes = 0;
      for (const entry of entries) {
        const existing = await requestResult(store.get(entry.key));
        if (!existing) continue;
        const bytes = Math.max(0, Number(existing.byteSize) || 0);
        store.delete(entry.key);
        deletedRecords += 1;
        freedBytes += bytes;
      }
      stats.totalBytes = Math.max(0, stats.totalBytes - freedBytes);
      stats.categoryBytes[category] = Math.max(
        0,
        stats.categoryBytes[category] - freedBytes,
      );
      stats.categoryCounts[category] = Math.max(
        0,
        stats.categoryCounts[category] - deletedRecords,
      );
      stats.updatedAt = now();
      meta.put(stats);
      await transactionResult(transaction);
      return { deletedRecords, freedBytes };
    }

    async function referencedMessageTokens() {
      const database = await open();
      const transaction = database.transaction(["segments", "searchSnapshots"], "readonly");
      const [segments, snapshots] = await Promise.all([
        requestResult(transaction.objectStore("segments").getAll()),
        requestResult(transaction.objectStore("searchSnapshots").getAll()),
      ]);
      await transactionResult(transaction);
      const tokens = new Set();
      for (const segment of segments) {
        for (const messageId of segment.messageIds || []) {
          tokens.add(keyToken("messages", [segment.conversationId, messageId]));
        }
      }
      for (const snapshot of snapshots) {
        for (const ref of snapshot.resultRefs || []) {
          if (ref?.conversationId && ref?.messageId) {
            tokens.add(keyToken("messages", [ref.conversationId, ref.messageId]));
          }
        }
      }
      return tokens;
    }

    async function recordCapacity(capacity, token) {
      const database = await open();
      const transaction = database.transaction(META_STORE, "readwrite");
      const meta = transaction.objectStore(META_STORE);
      await assertAndRenewLease(meta, token);
      meta.put({ key: "quota", ...capacity });
      await transactionResult(transaction);
    }

    async function setOperationState(key, value, token) {
      const database = await open();
      const transaction = database.transaction(META_STORE, "readwrite");
      const meta = transaction.objectStore(META_STORE);
      await assertAndRenewLease(meta, token);
      meta.put({ key, ...value, updatedAt: now() });
      await transactionResult(transaction);
    }

    async function cleanupCore(
      token,
      {
        emergency = false,
        signal = null,
        protectedKeys = [],
        batchSize = DEFAULT_BATCH_SIZE,
      } = {},
    ) {
      const capacity = await estimateCapacity();
      await recordCapacity(capacity, token);
      const ratio = emergency ? 0.75 : 0.90;
      const targetBytes = Math.floor(capacity.effectiveTarget * ratio);
      const protectedTokens = new Set(protectedKeys.map((entry) => (
        typeof entry === "string" ? entry : keyToken(entry.category, entry.key)
      )));
      for (const tokenized of await activeReaderProtectedTokens()) {
        protectedTokens.add(tokenized);
      }
      const previous = await getMeta("cleanup");
      const previousPhase = previous?.state === "interrupted" ? previous.phase : null;
      let phaseIndex = Math.max(
        0,
        CLEANUP_PHASES.findIndex((phase) => phase.id === previousPhase),
      );
      let deletedRecords = Number(previous?.deletedRecords) || 0;
      let freedBytes = Number(previous?.freedBytes) || 0;
      await setOperationState("cleanup", {
        state: "running",
        emergency,
        phase: CLEANUP_PHASES[phaseIndex].id,
        targetBytes,
        deletedRecords,
        freedBytes,
        startedAt: Number(previous?.startedAt) || now(),
      }, token);

      try {
        let stats = await getStats();
        if (stats.totalBytes <= targetBytes) {
          await setOperationState("cleanup", {
            state: "idle",
            emergency,
            phase: "complete",
            targetBytes,
            deletedRecords,
            freedBytes,
            completedAt: now(),
          }, token);
          return { ...stats, deletedRecords, freedBytes, targetBytes };
        }
        const referenced = await referencedMessageTokens();
        for (; phaseIndex < CLEANUP_PHASES.length; phaseIndex += 1) {
          const phase = CLEANUP_PHASES[phaseIndex];
          await setOperationState("cleanup", {
            state: "running",
            emergency,
            phase: phase.id,
            targetBytes,
            deletedRecords,
            freedBytes,
          }, token);
          const categories = phase.category === "*" ? DATA_STORES : [phase.category];
          for (const category of categories) {
            while (stats.totalBytes > targetBytes) {
              if (signal?.aborted) {
                await setOperationState("cleanup", {
                  state: "interrupted",
                  emergency,
                  phase: phase.id,
                  targetBytes,
                  deletedRecords,
                  freedBytes,
                }, token);
                return { ...stats, interrupted: true, deletedRecords, freedBytes, targetBytes };
              }
              const expiredRange = phase.id === "expired"
                ? scope.IDBKeyRange.upperBound(now())
                : null;
              const batch = await collectBatch(category, {
                indexName: phase.id === "expired"
                  ? "by_expires_at"
                  : "by_last_accessed_at",
                range: expiredRange,
                limit: batchSize,
                predicate: (record) => {
                  const recordKey = primaryKeyFor(category, record);
                  const tokenized = keyToken(category, recordKey);
                  if (protectedTokens.has(tokenized)) return false;
                  if (phase.kind && record.kind !== phase.kind) return false;
                  if (phase.orphanOnly && referenced.has(tokenized)) return false;
                  return true;
                },
              });
              if (!batch.length) break;
              const deleted = await deleteBatch(category, batch, token);
              deletedRecords += deleted.deletedRecords;
              freedBytes += deleted.freedBytes;
              stats = await getStats();
              await setOperationState("cleanup", {
                state: "running",
                emergency,
                phase: phase.id,
                targetBytes,
                deletedRecords,
                freedBytes,
              }, token);
              await yieldControl();
            }
            if (stats.totalBytes <= targetBytes) break;
          }
          if (stats.totalBytes <= targetBytes) break;
        }
        stats = await getStats();
        await setOperationState("cleanup", {
          state: "idle",
          emergency,
          phase: "complete",
          targetBytes,
          deletedRecords,
          freedBytes,
          completedAt: now(),
        }, token);
        return { ...stats, deletedRecords, freedBytes, targetBytes };
      } catch (error) {
        await setOperationState("cleanup", {
          state: "failed",
          emergency,
          phase: CLEANUP_PHASES[phaseIndex]?.id || "unknown",
          targetBytes,
          deletedRecords,
          freedBytes,
          errorCode: error?.code || error?.name || "cleanup_failed",
        }, token).catch(() => {});
        throw error;
      }
    }

    async function cleanup(options = {}) {
      return withLease((token) => cleanupCore(token, options));
    }

    async function putRecord(category, input, optionsForWrite = {}) {
      const record = prepareRecord(category, input);
      return withLease(async (token) => {
        const capacity = await estimateCapacity();
        await recordCapacity(capacity, token);
        let stats = await getStats();
        if (stats.totalBytes + record.byteSize > capacity.effectiveCeiling) {
          await cleanupCore(token, {
            emergency: false,
            signal: optionsForWrite.signal || null,
            protectedKeys: optionsForWrite.protectedKeys || [],
          });
          stats = await getStats();
        }
        if (stats.totalBytes + record.byteSize > capacity.effectiveCeiling) {
          throw new CapacityError();
        }
        for (let attempt = 0; attempt < 2; attempt += 1) {
          try {
            return await putRecordOnce(category, record, token, attempt);
          } catch (error) {
            if (!isQuotaExceeded(error) || attempt === 1) throw error;
            await cleanupCore(token, {
              emergency: true,
              signal: optionsForWrite.signal || null,
              protectedKeys: optionsForWrite.protectedKeys || [],
            });
          }
        }
        throw new PrivateCacheError("Private cache write retry state is invalid.");
      });
    }

    async function recomputeStats(token) {
      const database = await open();
      const at = now();
      const next = emptyStats(at);
      for (const category of DATA_STORES) {
        const transaction = database.transaction(category, "readonly");
        const records = await requestResult(transaction.objectStore(category).getAll());
        await transactionResult(transaction);
        for (const record of records) {
          next.categoryCounts[category] += 1;
          next.categoryBytes[category] += Math.max(0, Number(record.byteSize) || 0);
        }
        next.totalBytes += next.categoryBytes[category];
        await yieldControl();
      }
      next.lastReconciledAt = at;
      const transaction = database.transaction(META_STORE, "readwrite");
      const meta = transaction.objectStore(META_STORE);
      await assertAndRenewLease(meta, token);
      meta.put(next);
      await transactionResult(transaction);
      return next;
    }

    function mediaRecordIsComplete(record) {
      const BlobConstructor = scope.Blob;
      if (!BlobConstructor || !(record.blob instanceof BlobConstructor)) return false;
      if (record.complete !== true) return false;
      if (
        Number.isFinite(Number(record.contentLength))
        && Number(record.contentLength) !== record.blob.size
      ) return false;
      return true;
    }

    async function repairRecord(category, record, token) {
      if (category === "thumbnails" || category === "mediaObjects") {
        if (!mediaRecordIsComplete(record)) {
          return deleteBatch(category, [{
            key: primaryKeyFor(category, record),
            value: record,
          }], token);
        }
      }
      if (category === "segments") {
        const database = await open();
        const transaction = database.transaction("messages", "readonly");
        const messages = transaction.objectStore("messages");
        const missing = [];
        for (const messageId of record.messageIds || []) {
          if (!await requestResult(messages.get([record.conversationId, messageId]))) {
            missing.push(messageId);
          }
        }
        await transactionResult(transaction);
        const next = {
          ...record,
          partial: missing.length > 0,
          missingMessageIds: missing,
        };
        next.byteSize = logicalByteSize(scope, next);
        if (
          next.partial !== record.partial
          || JSON.stringify(next.missingMessageIds) !== JSON.stringify(record.missingMessageIds || [])
          || next.byteSize !== record.byteSize
        ) {
          await putRecordOnce(category, next, token, 0);
        }
        return { repairedRecords: 1, deletedRecords: 0 };
      }
      if (category === "searchSnapshots") {
        const strictSnapshot = record.snapshotVersion === SEARCH_SNAPSHOT_VERSION;
        const database = await open();
        const transaction = database.transaction(["messages", "segments"], "readonly");
        const messages = transaction.objectStore("messages");
        const segments = await requestResult(transaction.objectStore("segments").getAll());
        const validMessageTokens = new Set();
        for (const segment of segments) {
          if (
            segment?.contentVersion !== 1
            || segment.partial === true
            || Number(segment.expiresAt) <= now()
            || !Array.isArray(segment.messageIds)
            || !segment.messageIds.length
            || new Set(segment.messageIds).size !== segment.messageIds.length
          ) continue;
          for (const messageId of segment.messageIds) {
            validMessageTokens.add(
              keyToken("messages", [segment.conversationId, messageId]),
            );
          }
        }
        const kept = [];
        const seen = new Set();
        for (const ref of record.resultRefs || []) {
          const token = ref?.conversationId && ref?.messageId
            ? keyToken("messages", [ref.conversationId, ref.messageId])
            : "";
          if (
            token
            && !seen.has(token)
            && (!strictSnapshot || validMessageTokens.has(token))
            && await requestResult(messages.get([ref.conversationId, ref.messageId]))
          ) {
            seen.add(token);
            kept.push(ref);
          }
        }
        await transactionResult(transaction);
        if (strictSnapshot && !kept.length) {
          return deleteBatch(category, [{
            key: primaryKeyFor(category, record),
            value: record,
          }], token);
        }
        if (kept.length !== (record.resultRefs || []).length) {
          const next = { ...record, resultRefs: kept, partial: true };
          next.byteSize = logicalByteSize(scope, next);
          await putRecordOnce(category, next, token, 0);
        }
        return { repairedRecords: 1, deletedRecords: 0 };
      }
      const expectedBytes = logicalByteSize(scope, record);
      if (expectedBytes !== record.byteSize) {
        await putRecordOnce(category, { ...record, byteSize: expectedBytes }, token, 0);
      }
      return { repairedRecords: 1, deletedRecords: 0 };
    }

    async function repair({ signal = null, batchSize = DEFAULT_BATCH_SIZE } = {}) {
      return withLease(async (token) => {
        const previous = await getMeta("repair");
        let categoryIndex = Math.max(
          0,
          DATA_STORES.indexOf(previous?.state === "interrupted" ? previous.category : ""),
        );
        let repairedRecords = Number(previous?.repairedRecords) || 0;
        let deletedRecords = Number(previous?.deletedRecords) || 0;
        await setOperationState("repair", {
          state: "running",
          category: DATA_STORES[categoryIndex],
          repairedRecords,
          deletedRecords,
          startedAt: Number(previous?.startedAt) || now(),
        }, token);
        try {
          for (; categoryIndex < DATA_STORES.length; categoryIndex += 1) {
            const category = DATA_STORES[categoryIndex];
            const database = await open();
            const keysTransaction = database.transaction(category, "readonly");
            const keys = await requestResult(
              keysTransaction.objectStore(category).getAllKeys(),
            );
            await transactionResult(keysTransaction);
            for (let offset = 0; offset < keys.length; offset += batchSize) {
              if (signal?.aborted) {
                await setOperationState("repair", {
                  state: "interrupted",
                  category,
                  repairedRecords,
                  deletedRecords,
                }, token);
                return { interrupted: true, repairedRecords, deletedRecords };
              }
              const transaction = database.transaction(category, "readonly");
              const store = transaction.objectStore(category);
              const batchKeys = keys.slice(offset, offset + batchSize);
              const batch = await Promise.all(
                batchKeys.map((key) => requestResult(store.get(key))),
              );
              await transactionResult(transaction);
              for (const record of batch.filter(Boolean)) {
                const result = await repairRecord(category, record, token);
                repairedRecords += Number(result?.repairedRecords) || 0;
                deletedRecords += Number(result?.deletedRecords) || 0;
              }
              await setOperationState("repair", {
                state: "running",
                category,
                repairedRecords,
                deletedRecords,
              }, token);
              await yieldControl();
            }
          }
          const stats = await recomputeStats(token);
          await setOperationState("repair", {
            state: "idle",
            category: "complete",
            repairedRecords,
            deletedRecords,
            completedAt: now(),
          }, token);
          return { repairedRecords, deletedRecords, stats };
        } catch (error) {
          await setOperationState("repair", {
            state: "failed",
            category: DATA_STORES[categoryIndex] || "unknown",
            repairedRecords,
            deletedRecords,
            errorCode: error?.code || error?.name || "repair_failed",
          }, token).catch(() => {});
          throw error;
        }
      });
    }

    function normalizeMessageWindow(input) {
      if (!input || typeof input !== "object" || Array.isArray(input)) {
        throw new PrivateCacheError(
          "Message windows must be objects.",
          "invalid_message_window",
        );
      }
      const at = now();
      const conversationId = requiredIdentifier(input.conversationId, "conversationId");
      const direction = ["preview", "initial", "before", "after"].includes(input.direction)
        ? input.direction
        : "initial";
      const rawMessages = Array.isArray(input.messages) ? input.messages : [];
      if (!rawMessages.length) {
        throw new PrivateCacheError(
          "Message windows must contain at least one message.",
          "invalid_message_window",
        );
      }
      const messagesById = new Map();
      const messageOrder = [];
      for (const rawMessage of rawMessages) {
        if (!rawMessage || typeof rawMessage !== "object" || Array.isArray(rawMessage)) {
          throw new PrivateCacheError("Invalid message object.", "invalid_message_window");
        }
        const messageId = requiredIdentifier(
          rawMessage.messageId ?? rawMessage.message_id,
          "messageId",
        );
        const messageConversationId = String(
          rawMessage.conversationId ?? rawMessage.conversation_id ?? conversationId,
        ).trim();
        if (messageConversationId !== conversationId) {
          throw new PrivateCacheError(
            "Message conversationId does not match the window.",
            "invalid_message_window",
          );
        }
        const messageText = rawMessage.text;
        if (typeof messageText !== "string") {
          throw new PrivateCacheError(
            "Message text must be a string.",
            "invalid_message_window",
          );
        }
        if (!messagesById.has(messageId)) messageOrder.push(messageId);
        const position = finitePosition(rawMessage.position);
        messagesById.set(messageId, prepareRecord("messages", {
          conversationId,
          messageId,
          role: String(rawMessage.role || ""),
          text: messageText,
          messageTime: String(
            rawMessage.messageTime ?? rawMessage.create_time_iso ?? "",
          ),
          ...(position === null ? {} : { position }),
          cachedAt: at,
          validatedAt: at,
          expiresAt: at + TTL_MS,
          lastAccessedAt: at,
          contentVersion: 1,
        }));
      }
      const messages = messageOrder.map((messageId) => messagesById.get(messageId));
      const firstMessage = messages[0];
      const lastMessage = messages[messages.length - 1];
      const anchorMessageId = String(input.anchorMessageId || "").trim();
      if (anchorMessageId && !messagesById.has(anchorMessageId)) {
        throw new PrivateCacheError(
          "The message window does not contain its anchor.",
          "invalid_message_window",
        );
      }
      const conversationTitle = String(input.conversationTitle || "").trim();
      const conversation = prepareRecord("conversations", {
        conversationId,
        title: conversationTitle,
        cachedAt: at,
        validatedAt: at,
        expiresAt: at + TTL_MS,
        lastAccessedAt: at,
        contentVersion: 1,
      });
      const segment = prepareRecord("segments", {
        segmentKey: String(input.segmentKey || segmentKeyFor({
          conversationId,
          direction,
          firstMessageId: firstMessage.messageId,
          lastMessageId: lastMessage.messageId,
          anchorMessageId,
        })),
        conversationId,
        conversationTitle,
        firstMessageId: firstMessage.messageId,
        lastMessageId: lastMessage.messageId,
        messageIds: messageOrder,
        anchorMessageId,
        direction,
        requestCursor: String(input.requestCursor || ""),
        cursorBefore: String(input.cursorBefore || ""),
        cursorAfter: String(input.cursorAfter || ""),
        reachedStart: boundaryValue(input.reachedStart),
        reachedEnd: boundaryValue(input.reachedEnd),
        expectedMessageCount: Math.max(
          messageOrder.length,
          Number(input.expectedMessageCount) || messageOrder.length,
        ),
        firstPosition: firstMessage.position ?? null,
        lastPosition: lastMessage.position ?? null,
        partial: false,
        missingMessageIds: [],
        cachedAt: at,
        validatedAt: at,
        expiresAt: at + TTL_MS,
        lastAccessedAt: at,
        contentVersion: 1,
      });
      return { conversation, messages, segment };
    }

    function applyRecordStats(stats, category, next, previous) {
      const oldBytes = Math.max(0, Number(previous?.byteSize) || 0);
      const delta = next.byteSize - oldBytes;
      stats.totalBytes = Math.max(0, stats.totalBytes + delta);
      stats.categoryBytes[category] = Math.max(
        0,
        stats.categoryBytes[category] + delta,
      );
      if (!previous) stats.categoryCounts[category] += 1;
    }

    async function saveMessageWindowOnce(window, token, attempt) {
      writeAttemptHook?.({ category: "messageWindow", record: window.segment, attempt });
      const database = await open();
      const transaction = database.transaction(
        [META_STORE, "conversations", "messages", "segments"],
        "readwrite",
      );
      const meta = transaction.objectStore(META_STORE);
      const conversations = transaction.objectStore("conversations");
      const messages = transaction.objectStore("messages");
      const segments = transaction.objectStore("segments");
      try {
        await assertAndRenewLease(meta, token);
        const stats = normalizedStats(await requestResult(meta.get("stats")), now());
        const existingConversation = await requestResult(
          conversations.get(window.conversation.conversationId),
        );
        const conversation = {
          ...window.conversation,
          title: window.conversation.title || existingConversation?.title || "",
        };
        conversation.byteSize = logicalByteSize(scope, conversation);
        conversations.put(conversation);
        applyRecordStats(stats, "conversations", conversation, existingConversation);
        for (const message of window.messages) {
          const key = [message.conversationId, message.messageId];
          const existingMessage = await requestResult(messages.get(key));
          messages.put(message);
          applyRecordStats(stats, "messages", message, existingMessage);
        }
        messageWindowMutationHook?.({
          transaction,
          conversation,
          messages: window.messages,
          segment: window.segment,
          attempt,
        });
        const existingSegment = await requestResult(
          segments.get(window.segment.segmentKey),
        );
        segments.put(window.segment);
        applyRecordStats(stats, "segments", window.segment, existingSegment);
        stats.updatedAt = now();
        meta.put(stats);
        await transactionResult(transaction);
        return cloneForWrite(scope, {
          conversation,
          messages: window.messages,
          segment: window.segment,
        });
      } catch (error) {
        try {
          transaction.abort();
        } catch {
          // The transaction may already have completed its abort.
        }
        try {
          await transactionResult(transaction);
        } catch {
          // Preserve the original failure.
        }
        throw error;
      }
    }

    async function saveMessageWindow(input, optionsForWrite = {}) {
      const window = normalizeMessageWindow(input);
      const incomingBytes = window.conversation.byteSize
        + window.segment.byteSize
        + window.messages.reduce((total, message) => total + message.byteSize, 0);
      return withLease(async (token) => {
        const capacity = await estimateCapacity();
        await recordCapacity(capacity, token);
        let stats = await getStats();
        if (stats.totalBytes + incomingBytes > capacity.effectiveCeiling) {
          await cleanupCore(token, {
            emergency: false,
            signal: optionsForWrite.signal || null,
            protectedKeys: optionsForWrite.protectedKeys || [],
          });
          stats = await getStats();
        }
        if (stats.totalBytes + incomingBytes > capacity.effectiveCeiling) {
          throw new CapacityError();
        }
        for (let attempt = 0; attempt < 2; attempt += 1) {
          try {
            return await saveMessageWindowOnce(window, token, attempt);
          } catch (error) {
            if (!isQuotaExceeded(error) || attempt === 1) throw error;
            await cleanupCore(token, {
              emergency: true,
              signal: optionsForWrite.signal || null,
              protectedKeys: optionsForWrite.protectedKeys || [],
            });
          }
        }
        throw new PrivateCacheError("Message window retry state is invalid.");
      });
    }

    async function recordsForConversation(category, conversationId) {
      const database = await open();
      const transaction = database.transaction(category, "readonly");
      const store = transaction.objectStore(category);
      const records = await requestResult(
        store.index("by_conversation_id").getAll(scope.IDBKeyRange.only(conversationId)),
      );
      await transactionResult(transaction);
      return records;
    }

    async function readCompleteSegment(segment) {
      if (
        !segment
        || segment.contentVersion !== 1
        || segment.partial === true
        || Number(segment.expiresAt) <= now()
        || !Array.isArray(segment.messageIds)
        || !segment.messageIds.length
        || new Set(segment.messageIds).size !== segment.messageIds.length
      ) return null;
      const database = await open();
      const transaction = database.transaction("messages", "readonly");
      const store = transaction.objectStore("messages");
      const messages = [];
      for (const messageId of segment.messageIds) {
        const message = await requestResult(
          store.get([segment.conversationId, messageId]),
        );
        if (
          !message
          || message.contentVersion !== 1
          || Number(message.expiresAt) <= now()
          || typeof message.text !== "string"
        ) {
          await transactionResult(transaction);
          return null;
        }
        messages.push(message);
      }
      await transactionResult(transaction);
      return { segment, messages };
    }

    async function touchMessageWindow({
      conversationId,
      segmentKeys = [],
      messageIds = [],
    }) {
      conversationId = requiredIdentifier(conversationId, "conversationId");
      return withLease(async (token) => {
        const database = await open();
        const transaction = database.transaction(
          [META_STORE, "conversations", "messages", "segments"],
          "readwrite",
        );
        const meta = transaction.objectStore(META_STORE);
        await assertAndRenewLease(meta, token);
        const at = now();
        const conversationStore = transaction.objectStore("conversations");
        const conversation = await requestResult(conversationStore.get(conversationId));
        if (conversation && Number(conversation.expiresAt) > at) {
          conversation.lastAccessedAt = at;
          conversationStore.put(conversation);
        }
        const messageStore = transaction.objectStore("messages");
        for (const messageId of new Set(messageIds)) {
          const message = await requestResult(messageStore.get([conversationId, messageId]));
          if (message && Number(message.expiresAt) > at) {
            message.lastAccessedAt = at;
            messageStore.put(message);
          }
        }
        const segmentStore = transaction.objectStore("segments");
        for (const segmentKey of new Set(segmentKeys)) {
          const segment = await requestResult(segmentStore.get(segmentKey));
          if (segment && Number(segment.expiresAt) > at) {
            segment.lastAccessedAt = at;
            segmentStore.put(segment);
          }
        }
        await transactionResult(transaction);
        return { touchedAt: at };
      });
    }

    async function getCachedMessageContext({
      conversationId,
      anchorMessageId,
      expectedMessageCount = 5,
      touch = true,
    }) {
      conversationId = requiredIdentifier(conversationId, "conversationId");
      anchorMessageId = requiredIdentifier(anchorMessageId, "anchorMessageId");
      const segments = (await recordsForConversation("segments", conversationId))
        .filter((segment) => (
          segment.direction === "preview"
          && (
            segment.anchorMessageId === anchorMessageId
            || segment.messageIds?.includes(anchorMessageId)
          )
        ))
        .sort((left, right) => (
          Number(right.anchorMessageId === anchorMessageId)
          - Number(left.anchorMessageId === anchorMessageId)
          || Number(right.validatedAt) - Number(left.validatedAt)
        ));
      if (!segments.length) return { status: "missing" };
      const validSegments = segments.filter((segment) => Number(segment.expiresAt) > now());
      if (!validSegments.length) return { status: "expired" };
      for (const segment of validSegments) {
        if (
          Number(segment.expectedMessageCount) !== Number(expectedMessageCount)
          || segment.messageIds.length !== Number(expectedMessageCount)
        ) continue;
        const complete = await readCompleteSegment(segment);
        if (!complete) continue;
        if (touch) {
          await touchMessageWindow({
            conversationId,
            segmentKeys: [segment.segmentKey],
            messageIds: segment.messageIds,
          }).catch(() => {});
        }
        return {
          status: "ready",
          conversationId,
          conversationTitle: segment.conversationTitle || "",
          anchorMessageId,
          cachedAt: segment.cachedAt,
          validatedAt: segment.validatedAt,
          expiresAt: segment.expiresAt,
          messages: complete.messages,
          segmentKey: segment.segmentKey,
        };
      }
      return { status: "incomplete" };
    }

    async function getCachedConversationWindow({
      conversationId,
      anchorMessageId = "",
      direction = "",
      requestCursor = "",
      touch = true,
    }) {
      conversationId = requiredIdentifier(conversationId, "conversationId");
      anchorMessageId = String(anchorMessageId || "").trim();
      const segments = await recordsForConversation("segments", conversationId);
      if (!segments.length) return { status: "missing" };
      const matching = segments.filter((segment) => {
        if (direction && requestCursor) {
          return segment.direction === direction && segment.requestCursor === requestCursor;
        }
        if (anchorMessageId) return segment.messageIds?.includes(anchorMessageId);
        return true;
      });
      if (!matching.length) return { status: "missing" };
      if (!matching.some((segment) => Number(segment.expiresAt) > now())) {
        return { status: "expired" };
      }
      const complete = [];
      for (const segment of segments) {
        const loaded = await readCompleteSegment(segment);
        if (loaded) complete.push(loaded);
      }
      const matchingKeys = new Set(matching.map((segment) => segment.segmentKey));
      if (!complete.some(({ segment }) => matchingKeys.has(segment.segmentKey))) {
        return { status: "incomplete" };
      }
      complete.sort((left, right) => {
        const leftPosition = finitePosition(left.segment.firstPosition);
        const rightPosition = finitePosition(right.segment.firstPosition);
        if (leftPosition !== null && rightPosition !== null) {
          return leftPosition - rightPosition;
        }
        return Number(left.segment.cachedAt) - Number(right.segment.cachedAt);
      });
      const messages = [];
      const seenMessageIds = new Set();
      const gapsBeforeMessageIds = [];
      let coverageLastPosition = null;
      for (const entry of complete) {
        if (messages.length) {
          const currentFirst = finitePosition(entry.segment.firstPosition);
          const overlaps = entry.segment.messageIds.some(
            (messageId) => seenMessageIds.has(messageId),
          );
          if (
            !overlaps
            && (
              coverageLastPosition === null
              || currentFirst === null
              || currentFirst > coverageLastPosition + 1
            )
          ) {
            gapsBeforeMessageIds.push(entry.segment.firstMessageId);
          }
        }
        for (const message of entry.messages) {
          if (seenMessageIds.has(message.messageId)) continue;
          seenMessageIds.add(message.messageId);
          messages.push({
            ...message,
            isFocus: Boolean(anchorMessageId && message.messageId === anchorMessageId),
          });
        }
        const currentLast = finitePosition(entry.segment.lastPosition);
        if (currentLast !== null) {
          coverageLastPosition = coverageLastPosition === null
            ? currentLast
            : Math.max(coverageLastPosition, currentLast);
        }
      }
      if (touch) {
        await touchMessageWindow({
          conversationId,
          segmentKeys: complete.map(({ segment }) => segment.segmentKey),
          messageIds: [...seenMessageIds],
        }).catch(() => {});
      }
      const first = complete[0].segment;
      const last = complete[complete.length - 1].segment;
      return {
        status: "ready",
        conversationId,
        conversationTitle: complete
          .map(({ segment }) => segment.conversationTitle)
          .find(Boolean) || "",
        anchorMessageId,
        messages,
        segmentKeys: complete.map(({ segment }) => segment.segmentKey),
        gapsBeforeMessageIds,
        reachedStart: boundaryValue(first.reachedStart),
        reachedEnd: boundaryValue(last.reachedEnd),
        cachedAt: Math.max(...complete.map(({ segment }) => Number(segment.cachedAt) || 0)),
        validatedAt: Math.max(
          ...complete.map(({ segment }) => Number(segment.validatedAt) || 0),
        ),
        expiresAt: Math.min(
          ...complete.map(({ segment }) => Number(segment.expiresAt) || 0),
        ),
      };
    }

    async function getCachedConversationCatalog() {
      const database = await open();
      const transaction = database.transaction("segments", "readonly");
      const segments = await requestResult(transaction.objectStore("segments").getAll());
      await transactionResult(transaction);
      const conversationIds = [...new Set(
        segments
          .filter((segment) => Number(segment.expiresAt) > now())
          .map((segment) => segment.conversationId)
          .filter(Boolean),
      )];
      const catalog = [];
      for (const conversationId of conversationIds) {
        const window = await getCachedConversationWindow({
          conversationId,
          touch: false,
        });
        if (window.status !== "ready" || !window.messages.length) continue;
        const conversation = await getRecord("conversations", conversationId);
        const lastAccessedAt = Math.max(
          Number(conversation?.lastAccessedAt) || 0,
          ...segments
            .filter((segment) => segment.conversationId === conversationId)
            .map((segment) => Number(segment.lastAccessedAt) || 0),
        );
        catalog.push({
          conversationId,
          conversationTitle: window.conversationTitle
            || conversation?.title
            || "未命名会话",
          anchorMessageId: window.anchorMessageId
            || window.messages.find((message) => message.isFocus)?.messageId
            || window.messages[0].messageId,
          cachedAt: window.cachedAt,
          lastAccessedAt,
          messageCount: window.messages.length,
        });
      }
      return catalog.sort(
        (left, right) => Number(right.lastAccessedAt) - Number(left.lastAccessedAt),
      );
    }

    async function markActiveReaderWindow({
      conversationId,
      segmentKeys = [],
      messageIds = [],
    }) {
      conversationId = requiredIdentifier(conversationId, "conversationId");
      return withLease(async (token) => {
        const database = await open();
        const transaction = database.transaction(META_STORE, "readwrite");
        const meta = transaction.objectStore(META_STORE);
        await assertAndRenewLease(meta, token);
        const at = now();
        const protectedTokens = [
          keyToken("conversations", conversationId),
          ...segmentKeys.map((key) => keyToken("segments", key)),
          ...messageIds.map((messageId) => (
            keyToken("messages", [conversationId, messageId])
          )),
        ];
        meta.put({
          key: `activeReader:${ownerId}`,
          conversationId,
          protectedTokens: [...new Set(protectedTokens)],
          expiresAt: at + ACTIVE_READER_PROTECTION_MS,
          updatedAt: at,
        });
        await transactionResult(transaction);
        return { conversationId, expiresAt: at + ACTIVE_READER_PROTECTION_MS };
      });
    }

    async function releaseActiveReaderWindow() {
      return withLease(async (token) => {
        const database = await open();
        const transaction = database.transaction(META_STORE, "readwrite");
        const meta = transaction.objectStore(META_STORE);
        await assertAndRenewLease(meta, token);
        meta.delete(`activeReader:${ownerId}`);
        await transactionResult(transaction);
        return { released: true };
      });
    }

    function boundedResumeIdentifier(value) {
      const normalized = String(value || "").trim();
      return normalized && normalized.length <= 2048 ? normalized : "";
    }

    function normalizedResumeRefs(refs) {
      const seen = new Set();
      const result = [];
      for (const ref of Array.isArray(refs) ? refs : []) {
        const conversationId = boundedResumeIdentifier(ref?.conversationId);
        const messageId = boundedResumeIdentifier(ref?.messageId);
        if (!conversationId || !messageId) continue;
        const token = keyToken("messages", [conversationId, messageId]);
        if (seen.has(token)) continue;
        seen.add(token);
        result.push({ conversationId, messageId });
        if (result.length >= 5000) break;
      }
      return result;
    }

    function sanitizeResumeState(input, savedAt, cacheGeneration) {
      const route = ["home", "results", "reader"].includes(input?.route)
        ? input.route
        : "home";
      const sourceMode = input?.sourceMode === "cached-only" ? "cached-only" : "online";
      const query = String(input?.query || "").slice(0, 4096);
      const resultAnchorRef = normalizedResumeRefs([input?.resultAnchorRef])[0] || null;
      const expandedPreviewRef = normalizedResumeRefs([input?.expandedPreviewRef])[0] || null;
      return {
        key: RESUME_STATE_KEY,
        stateVersion: RESUME_STATE_VERSION,
        savedAt,
        lastInteractionAt: Number(input?.lastInteractionAt) || savedAt,
        route,
        sourceMode,
        query,
        sortMode: input?.sortMode === "latest" ? "latest" : "relevance",
        loadedResultRefs: normalizedResumeRefs(input?.loadedResultRefs),
        resultAnchorRef,
        resultAnchorOffset: Number(input?.resultAnchorOffset) || 0,
        resultScrollTop: Math.max(0, Number(input?.resultScrollTop) || 0),
        expandedConversationId: boundedResumeIdentifier(input?.expandedConversationId),
        expandedMatchIds: normalizedResumeRefs(input?.expandedMatchIds),
        expandedPreviewRef,
        currentConversationId: boundedResumeIdentifier(input?.currentConversationId),
        currentAnchorMessageId: boundedResumeIdentifier(input?.currentAnchorMessageId),
        readerScrollAnchorMessageId: boundedResumeIdentifier(
          input?.readerScrollAnchorMessageId,
        ),
        readerScrollOffset: Number(input?.readerScrollOffset) || 0,
        topBarHidden: Boolean(input?.topBarHidden),
        orientation: input?.orientation === "landscape" ? "landscape" : "portrait",
        pendingRefresh: Boolean(input?.pendingRefresh),
        cacheGeneration: String(cacheGeneration || ""),
        expiresAt: savedAt + RESUME_TTL_MS,
      };
    }

    async function readResumeStateRecord() {
      const database = await openResumeDatabase();
      const transaction = database.transaction(RESUME_STORE, "readonly");
      const record = await requestResult(
        transaction.objectStore(RESUME_STORE).get(RESUME_STATE_KEY),
      );
      await transactionResult(transaction);
      return record || null;
    }

    async function discardResumeState(reason = "discarded") {
      return withLease(async (token) => {
        const database = await openResumeDatabase();
        const mainDatabase = await open();
        const leaseTransaction = mainDatabase.transaction(META_STORE, "readwrite");
        await assertAndRenewLease(leaseTransaction.objectStore(META_STORE), token);
        await transactionResult(leaseTransaction);
        const transaction = database.transaction(RESUME_STORE, "readwrite");
        transaction.objectStore(RESUME_STORE).delete(RESUME_STATE_KEY);
        await transactionResult(transaction);
        return { discarded: true, reason };
      });
    }

    async function saveResumeState(input, { advanceSavedAt = true } = {}) {
      return withLease(async (token) => {
        const existing = await readResumeStateRecord().catch(() => null);
        const at = now();
        const savedAt = advanceSavedAt
          ? at
          : Number(input?.savedAt) || Number(existing?.savedAt) || 0;
        if (!savedAt) return { saved: false, reason: "no_interaction" };
        const generation = await getCacheGeneration();
        const state = sanitizeResumeState(input, savedAt, generation);
        const mainDatabase = await open();
        const leaseTransaction = mainDatabase.transaction(META_STORE, "readwrite");
        await assertAndRenewLease(leaseTransaction.objectStore(META_STORE), token);
        await transactionResult(leaseTransaction);
        const database = await openResumeDatabase();
        const transaction = database.transaction(RESUME_STORE, "readwrite");
        transaction.objectStore(RESUME_STORE).put(state);
        await transactionResult(transaction);
        return { saved: true, state };
      });
    }

    async function loadResumeState() {
      let state;
      try {
        state = await readResumeStateRecord();
      } catch (error) {
        return {
          status: "invalid",
          reason: error?.code || "resume_read_failed",
          state: null,
        };
      }
      if (!state) return { status: "missing", reason: "missing", state: null };
      const at = now();
      const savedAt = Number(state.savedAt);
      let reason = "";
      if (!state || typeof state !== "object" || Array.isArray(state)) {
        reason = "corrupt_state";
      } else if (state.stateVersion !== RESUME_STATE_VERSION) {
        reason = "incompatible_state_version";
      } else if (!Number.isFinite(savedAt) || savedAt <= 0) {
        reason = "invalid_saved_at";
      } else if (at + CLOCK_ROLLBACK_TOLERANCE_MS < savedAt) {
        reason = "clock_rollback";
      } else if (at - savedAt > RESUME_TTL_MS) {
        reason = "expired";
      } else if (!["home", "results", "reader"].includes(state.route)) {
        reason = "invalid_route";
      }
      const [migration, clock, generation] = await Promise.all([
        getMeta("migration"),
        getMeta("clock"),
        getCacheGeneration(),
      ]);
      if (!reason && migration?.state !== "idle") reason = "migration_incomplete";
      if (!reason && clock?.rollbackDetected) reason = "clock_rollback";
      if (!reason && state.cacheGeneration !== generation) reason = "cache_generation_changed";
      if (
        !reason
        && state.route === "reader"
        && (!boundedResumeIdentifier(state.currentConversationId)
          || !boundedResumeIdentifier(state.currentAnchorMessageId))
      ) {
        reason = "invalid_reader_reference";
      }
      let normalizedState = sanitizeResumeState(state, savedAt, generation);
      if (!reason && normalizedState.route === "results" && normalizedState.sourceMode === "cached-only") {
        try {
          const refreshed = await searchCachedMessages({
            query: normalizedState.query,
            sortMode: normalizedState.sortMode,
            limit: Math.max(20, normalizedState.loadedResultRefs.length),
          });
          const validTokens = new Set(
            refreshed.resultRefs.map(
              (ref) => keyToken("messages", [ref.conversationId, ref.messageId]),
            ),
          );
          normalizedState.loadedResultRefs = normalizedState.loadedResultRefs.filter(
            (ref) => validTokens.has(
              keyToken("messages", [ref.conversationId, ref.messageId]),
            ),
          );
          if (state.loadedResultRefs?.length && !normalizedState.loadedResultRefs.length) {
            reason = "cached_results_missing";
          }
        } catch (error) {
          reason = error?.name === "AbortError" ? "restore_cancelled" : "cached_results_invalid";
        }
      }
      if (!reason && normalizedState.route === "reader") {
        const cached = await getCachedConversationWindow({
          conversationId: normalizedState.currentConversationId,
          anchorMessageId: normalizedState.currentAnchorMessageId,
          touch: false,
        }).catch(() => ({ status: "incomplete" }));
        if (cached.status !== "ready") reason = `reader_${cached.status}`;
      }
      if (reason) {
        await discardResumeState(reason).catch(() => {});
        return {
          status: reason === "expired" ? "expired" : "invalid",
          reason,
          state: null,
        };
      }
      return { status: "ready", reason: "", state: normalizedState };
    }

    async function clearPrivateCache({ clearResume = true, clearLegacyState = true } = {}) {
      const clearResult = await withLease(async (token) => {
        const database = await open();
        const transaction = database.transaction([META_STORE, ...DATA_STORES], "readwrite");
        const meta = transaction.objectStore(META_STORE);
        await assertAndRenewLease(meta, token);
        for (const category of DATA_STORES) {
          transaction.objectStore(category).clear();
        }
        const at = now();
        meta.put(emptyStats(at));
        meta.put({
          key: "cleanup",
          state: "idle",
          phase: "cleared",
          updatedAt: at,
        });
        meta.put({
          key: "repair",
          state: "idle",
          category: "cleared",
          updatedAt: at,
        });
        meta.put({
          key: "cacheGeneration",
          value: `cache-${stableOwnerId(scope)}`,
          updatedAt: at,
        });
        await transactionResult(transaction);
        return { privateContentCleared: true };
      });
      let resumeCleared = false;
      if (clearResume) {
        resumeConnection?.close();
        resumeConnection = null;
        resumeOpening = null;
        const result = await deleteDatabase(indexedDb, RESUME_DB_NAME);
        resumeCleared = result.deleted;
      }
      let legacyStateCleared = false;
      if (clearLegacyState) {
        try {
          scope.localStorage?.removeItem(LEGACY_MOBILE_HISTORY_KEY);
          legacyStateCleared = true;
        } catch {
          legacyStateCleared = false;
        }
      }
      return {
        ...clearResult,
        resumeCleared,
        legacyStateCleared,
        appShellPreserved: true,
      };
    }

    async function inspect() {
      const [schema, migration, stats, quota, cleanupState, repairState, clock, lease] =
        await Promise.all([
          getMeta("schema"),
          getMeta("migration"),
          getStats(),
          getMeta("quota"),
          getMeta("cleanup"),
          getMeta("repair"),
          getMeta("clock"),
          getMeta("writerLease"),
        ]);
      return {
        dbName,
        dbVersion,
        contentSchemaVersion: CONTENT_SCHEMA_VERSION,
        schema,
        migration,
        stats,
        quota,
        cleanup: cleanupState,
        repair: repairState,
        clock,
        writerLease: lease
          ? {
            ownerId: lease.ownerId,
            expiresAt: lease.expiresAt,
          }
          : null,
      };
    }

    return Object.freeze({
      open,
      close,
      inspect,
      estimateCapacity,
      requestPersistence,
      getRecord,
      putRecord,
      getCacheGeneration,
      saveMessageWindow,
      getCachedMessageContext,
      getCachedConversationWindow,
      getCachedConversationCatalog,
      searchCachedMessages,
      saveSearchSnapshot,
      getSearchSnapshot,
      saveResumeState,
      loadResumeState,
      discardResumeState,
      touchMessageWindow,
      markActiveReaderWindow,
      releaseActiveReaderWindow,
      cleanup,
      repair,
      clearPrivateCache,
      observeClock,
      constants: Object.freeze({
        dbName,
        dbVersion,
        resumeDbVersion: RESUME_DB_VERSION,
        contentSchemaVersion: CONTENT_SCHEMA_VERSION,
        searchSnapshotVersion: SEARCH_SNAPSHOT_VERSION,
        resumeStateVersion: RESUME_STATE_VERSION,
        resumeTtlMs: RESUME_TTL_MS,
        ttlMs: TTL_MS,
        dataStores: DATA_STORES,
        productTargetBytes: PRODUCT_TARGET_BYTES,
        productCeilingBytes: PRODUCT_CEILING_BYTES,
        fallbackTargetBytes: DEFAULT_TARGET_BYTES,
        fallbackCeilingBytes: DEFAULT_CEILING_BYTES,
      }),
    });
  }

  const namespace = Object.freeze({
    create: createPrivateCache,
    PrivateCacheError,
    LeaseBusyError,
    CapacityError,
    constants: Object.freeze({
      defaultDbName: DEFAULT_DB_NAME,
      resumeDbName: RESUME_DB_NAME,
      dbVersion: DB_VERSION,
      resumeDbVersion: RESUME_DB_VERSION,
      contentSchemaVersion: CONTENT_SCHEMA_VERSION,
      searchSnapshotVersion: SEARCH_SNAPSHOT_VERSION,
      resumeStateVersion: RESUME_STATE_VERSION,
      resumeTtlMs: RESUME_TTL_MS,
      ttlMs: TTL_MS,
      dataStores: DATA_STORES,
    }),
  });
  globalThis.YunOSPrivateCache = namespace;
  globalThis.yunosPrivateCache = createPrivateCache();
})();
