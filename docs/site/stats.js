/* shaliach.me visitor counting. No cookies, no storage, no account.
 *
 * Each page view bumps a few public counters on abacus (a free counter service,
 * no signup): views per day, views per page, and -- when the visitor arrived from
 * outside the site -- visits per day and which site sent them. bin/site-stats
 * reads them back.
 *
 * GoatCounter (free, privacy-friendly, adds unique visitors and a dashboard):
 * paste the site code below, e.g. "shaliach" for https://shaliach.goatcounter.com.
 */
var GOATCOUNTER = "";

(function () {
  var HOST = "shaliach.me";
  var COUNTER = "https://abacus.jasoncameron.dev/hit/" + HOST + "/";
  var BUCKETS = [
    ["linkedin", /(^|\.)(linkedin\.com|lnkd\.in)$/],
    ["hn", /(^|\.)ycombinator\.com$/],
    ["reddit", /(^|\.)(reddit\.com|redd\.it)$/],
    ["x", /(^|\.)(t\.co|x\.com|twitter\.com)$/],
    ["github", /(^|\.)github\.com$/],
    ["google", /(^|\.)google\.[a-z.]+$/]
  ];
  var NAMES = { linkedin: 1, hn: 1, reddit: 1, x: 1, github: 1, google: 1 };

  function hostOf(url) {
    var m = /^[a-z]+:\/\/([^\/?#:]+)/i.exec(url || "");
    return m ? m[1].toLowerCase().replace(/^www\./, "") : "";
  }

  function bucket(referrer, url) {
    var tag = /[?&](?:utm_source|ref)=([a-z0-9_-]+)/i.exec(url);
    var host = hostOf(referrer);
    if (!host && tag) {
      var t = tag[1].toLowerCase();
      if (t === "twitter") t = "x";
      if (t === "ycombinator" || t === "hackernews") t = "hn";
      return NAMES[t] ? t : "other";
    }
    if (!host) return "direct";
    for (var i = 0; i < BUCKETS.length; i++) if (BUCKETS[i][1].test(host)) return BUCKETS[i][0];
    return "other";
  }

  function keysFor(url, referrer, day) {
    if (hostOf(url) !== HOST) return [];
    var path = url.replace(/^[a-z]+:\/\/[^\/]+/i, "").replace(/[?#].*$/, "");
    var page = path === "/" || path === "/index.html" ? "home"
      : (path.replace(/^\//, "").replace(/\.html$/, "").replace(/[^A-Za-z0-9_-]/g, "-") || "home");
    var keys = ["views-" + day, "page-" + page];
    if (hostOf(referrer) !== HOST) keys.push("visits-" + day, "ref-" + bucket(referrer, url));
    return keys;
  }

  function hit(key) {
    try { fetch(COUNTER + key, { keepalive: true }).catch(function () {}); } catch (e) {}
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { keysFor: keysFor };
    return;
  }

  var day = new Date().toISOString().slice(0, 10);
  keysFor(location.href, document.referrer, day).forEach(hit);
  if (location.hostname !== HOST) return;

  // Copying an install command is the closest thing to an install the page can see.
  document.addEventListener("copy", function () {
    var s = String(window.getSelection && window.getSelection());
    var what = /shaliach\.me\/install/.test(s) ? "copy-install-server"
      : /install-mac\.sh/.test(s) ? "copy-install-mac" : "";
    if (!what) return;
    hit(what);
    if (window.goatcounter && window.goatcounter.count) window.goatcounter.count({ path: what, event: true });
  });

  if (GOATCOUNTER) {
    var g = document.createElement("script");
    g.async = true;
    g.src = "https://gc.zgo.at/count.js";
    g.setAttribute("data-goatcounter", "https://" + GOATCOUNTER + ".goatcounter.com/count");
    document.head.appendChild(g);
  }
})();
