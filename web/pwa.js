(() => {
  "use strict";

  const hostStatus = document.querySelector("#pwa-host-status");
  const HOST_UNAVAILABLE_FALLBACK = (
    hostStatus?.textContent?.trim()
    || "无法连接 YunOS 主机。实时内容不可用，可继续阅读有效缓存副本。"
  );
  const HOST_STATUS_EXPANDED_MS = 3000;
  const HOST_STATUS_FADE_MS = 220;
  let registration = null;
  let reloadAfterActivation = false;
  let updateCheckInFlight = null;
  let hostAvailable = null;
  let hostStatusCollapseTimer = 0;
  let hostStatusFadeTimer = 0;

  function t(key, fallback) {
    if (
      !window.YunOSI18n
      || typeof window.YunOSI18n.t !== "function"
    ) return fallback;
    const translated = window.YunOSI18n.t(key, {}, fallback);
    return (
      typeof translated === "string"
      && translated
      && translated !== key
    ) ? translated : fallback;
  }

  function clearHostStatusTimers() {
    window.clearTimeout(hostStatusCollapseTimer);
    window.clearTimeout(hostStatusFadeTimer);
    hostStatusCollapseTimer = 0;
    hostStatusFadeTimer = 0;
  }

  function collapseHostStatus() {
    if (!hostStatus || hostAvailable !== false) return;
    hostStatus.classList.add("is-dismissing");
    hostStatus.dataset.displayMode = "dismissing";
    hostStatusFadeTimer = window.setTimeout(() => {
      if (hostAvailable !== false) return;
      hostStatus.classList.remove("is-dismissing");
      hostStatus.classList.add("is-compact");
      hostStatus.dataset.displayMode = "compact";
      hostStatusFadeTimer = 0;
    }, HOST_STATUS_FADE_MS);
  }

  function setHostAvailable(available) {
    const nextAvailable = available !== false;
    const availabilityChanged = hostAvailable !== nextAvailable;
    hostAvailable = nextAvailable;
    document.body.dataset.yunosHostAvailable = String(nextAvailable);
    if (!hostStatus) return;
    if (nextAvailable) {
      clearHostStatusTimers();
      hostStatus.hidden = true;
      hostStatus.classList.remove("is-dismissing", "is-compact");
      hostStatus.dataset.displayMode = "hidden";
      return;
    }
    hostStatus.textContent = t("pwa.hostUnavailable", HOST_UNAVAILABLE_FALLBACK);
    hostStatus.hidden = false;
    if (!availabilityChanged && hostStatus.dataset.displayMode) return;
    clearHostStatusTimers();
    hostStatus.classList.remove("is-dismissing", "is-compact");
    hostStatus.dataset.displayMode = "expanded";
    hostStatusCollapseTimer = window.setTimeout(() => {
      hostStatusCollapseTimer = 0;
      collapseHostStatus();
    }, HOST_STATUS_EXPANDED_MS);
  }

  window.addEventListener("yunos:host-connectivity", (event) => {
    setHostAvailable(event.detail?.available !== false);
  });
  window.addEventListener("offline", () => setHostAvailable(false));
  window.addEventListener("online", async () => {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      setHostAvailable(response.ok);
    } catch {
      setHostAvailable(false);
    }
  });
  if (document.body.dataset.yunosOfflineShell === "true" || !navigator.onLine) {
    setHostAvailable(false);
  }

  function announceWaitingWorker() {
    if (!registration?.waiting) return;
    window.dispatchEvent(new CustomEvent("yunos:pwa-update-ready", {
      detail: {
        activationRequired: true,
        message: t("pwa.updateReady", "YunOS 更新已就绪。"),
        actionLabel: t(
          "pwa.activateUpdate",
          t("common.reload", "重新加载"),
        ),
        laterLabel: t("pwa.updateLater", "稍后"),
      },
    }));
  }

  function observeInstallingWorker(worker) {
    if (!worker) return;
    worker.addEventListener("statechange", () => {
      if (worker.state === "installed" && navigator.serviceWorker.controller) {
        announceWaitingWorker();
      }
    });
  }

  async function checkForUpdate() {
    if (!registration || updateCheckInFlight) return updateCheckInFlight;
    updateCheckInFlight = registration.update()
      .finally(() => { updateCheckInFlight = null; });
    return updateCheckInFlight;
  }

  window.yunosPwa = Object.freeze({
    getUpdateState() {
      return Object.freeze({
        supported: "serviceWorker" in navigator,
        registered: Boolean(registration),
        updateReady: Boolean(registration?.waiting),
      });
    },
    checkForUpdate,
    activateUpdate({ reload = false } = {}) {
      if (!registration?.waiting) return false;
      reloadAfterActivation = Boolean(reload);
      registration.waiting.postMessage({ type: "YUNOS_ACTIVATE_UPDATE" });
      return true;
    },
  });

  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;

  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!reloadAfterActivation) return;
    reloadAfterActivation = false;
    window.location.reload();
  });

  navigator.serviceWorker.register("/service-worker.js", {
    scope: "/",
    updateViaCache: "none",
  }).then((nextRegistration) => {
    registration = nextRegistration;
    announceWaitingWorker();
    observeInstallingWorker(registration.installing);
    registration.addEventListener("updatefound", () => {
      observeInstallingWorker(registration.installing);
    });
    window.setTimeout(() => { checkForUpdate().catch(() => {}); }, 10000);
  }).catch((error) => {
    console.warn("YunOS PWA service worker registration failed; continuing without offline shell.", error);
  });
})();
