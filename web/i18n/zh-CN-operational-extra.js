(function registerYunOSSimplifiedChineseOperationalExtraCatalog(root) {
  "use strict";

  const catalog = Object.freeze({
    "operational.extra.sectionWithCount": "{title}（{count}）",
  });

  if (!Object.prototype.hasOwnProperty.call(root, "YunOSI18nCatalogZhCNOperationalExtra")) {
    Object.defineProperty(root, "YunOSI18nCatalogZhCNOperationalExtra", {
      value: catalog,
      enumerable: false,
      configurable: false,
      writable: false,
    });
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
