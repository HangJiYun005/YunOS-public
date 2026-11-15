(function registerYunOSEnglishOperationalExtraCatalog(root) {
  "use strict";

  const catalog = Object.freeze({
    "operational.extra.sectionWithCount": "{title} ({count})",
  });

  if (!Object.prototype.hasOwnProperty.call(root, "YunOSI18nCatalogEnOperationalExtra")) {
    Object.defineProperty(root, "YunOSI18nCatalogEnOperationalExtra", {
      value: catalog,
      enumerable: false,
      configurable: false,
      writable: false,
    });
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
