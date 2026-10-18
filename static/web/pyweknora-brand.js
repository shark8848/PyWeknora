(function () {
  "use strict";

  var GITHUB_URL = "https://github.com/shark8848/PyWeknora";
  var LOGO_URL = "/assets/pyweknora-logo.svg";
  var OLD_LOGO_PATH = "weknora-BoLwP3xK.png";
  var OFFICIAL_HOST = "weknora.weixin.qq.com";
  var OLD_GITHUB_URL = "github.com/Tencent/WeKnora";
  var scheduled = false;

  function rewriteGithubUrl(value) {
    return String(value || "").replaceAll(OLD_GITHUB_URL, "github.com/shark8848/PyWeknora");
  }

  function installWindowOpenRewrite() {
    var originalOpen = window.open;
    window.open = function (url) {
      var args = Array.prototype.slice.call(arguments);
      if (typeof url === "string") args[0] = rewriteGithubUrl(url);
      return originalOpen.apply(window, args);
    };
  }

  function updateLoginHeader() {
    var logo = document.querySelector(".header-logo");
    if (logo) {
      logo.setAttribute("href", GITHUB_URL);
      logo.setAttribute("title", "PyWeKnora on GitHub");
      var image = logo.querySelector("img");
      if (image) {
        if (image.getAttribute("src") !== LOGO_URL) image.setAttribute("src", LOGO_URL);
        image.setAttribute("alt", "PyWeKnora");
      }
    }

    var links = document.querySelector(".header-links");
    if (!links) return;
    links.querySelectorAll("a[href]").forEach(function (link) {
      var href = link.getAttribute("href") || "";
      if (href.indexOf(OFFICIAL_HOST) !== -1) {
        link.remove();
        return;
      }
      if (href.indexOf(OLD_GITHUB_URL) !== -1) {
        link.setAttribute("href", rewriteGithubUrl(href));
        link.setAttribute("title", "PyWeKnora on GitHub");
      }
    });
  }

  function updatePortalImages(root) {
    root.querySelectorAll("img[src]").forEach(function (image) {
      var src = image.getAttribute("src") || "";
      if (src.indexOf(OLD_LOGO_PATH) !== -1) {
        image.setAttribute("src", LOGO_URL);
        image.setAttribute("alt", "PyWeKnora");
      }
    });
  }

  function updateGithubLinks(root) {
    root.querySelectorAll("a[href]").forEach(function (link) {
      var href = link.getAttribute("href") || "";
      if (href.indexOf(OFFICIAL_HOST) !== -1) {
        link.remove();
        return;
      }
      if (href.indexOf(OLD_GITHUB_URL) !== -1) {
        link.setAttribute("href", rewriteGithubUrl(href));
      }
    });
    root.querySelectorAll("pre, code").forEach(function (block) {
      if (block.textContent.indexOf(OLD_GITHUB_URL) === -1) return;
      var walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
      var node;
      while ((node = walker.nextNode())) {
        var value = node.nodeValue || "";
        var updated = rewriteGithubUrl(value);
        if (updated !== value) node.nodeValue = updated;
      }
    });
  }

  function updateLoginText() {
    if (window.location.pathname !== "/login") return;
    var form = document.querySelector(".form-card");
    if (!form) return;
    var walker = document.createTreeWalker(form, NodeFilter.SHOW_TEXT);
    var node;
    while ((node = walker.nextNode())) {
      var value = node.nodeValue || "";
      var updated = value.replace(/\bWeKnora\b/g, "PyWeKnora");
      if (updated !== value) node.nodeValue = updated;
    }
  }

  function applyBranding() {
    scheduled = false;
    updateLoginHeader();
    updatePortalImages(document);
    updateGithubLinks(document);
    updateLoginText();
  }

  function scheduleBranding() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(applyBranding);
  }

  installWindowOpenRewrite();

  new MutationObserver(scheduleBranding).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scheduleBranding, { once: true });
  } else {
    scheduleBranding();
  }
})();
