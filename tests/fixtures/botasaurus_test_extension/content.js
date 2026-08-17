// DOM attribute writes are visible across the isolated/main JS world split
// (only JS variables/objects are isolated, not the DOM itself) — avoids
// depending on script-tag injection, which a page's CSP could block.
document.documentElement.setAttribute("data-botasaurus-test-extension-loaded", "true");
