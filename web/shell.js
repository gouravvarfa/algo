/* Shared shell: theme (persistent, system-aware) + left sidebar navigation (icons, live index cards) + status bar.
   Loaded in <head> so the theme is applied before first paint. Every page keeps its own <nav data-shell> placeholder;
   the old per-page sidebar / top bar is replaced by this one shell. */
(function () {
  var KEY = "theme", root = document.documentElement;
  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function isDark() {
    var t = root.getAttribute("data-theme");
    return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function paint() {
    var d = isDark();
    document.querySelectorAll("[data-theme-btn]").forEach(function (b) { b.classList.toggle("on", (b.dataset.themeBtn === "dark") === d); });
  }
  function apply(t, notify) {
    if (t) root.setAttribute("data-theme", t); else root.removeAttribute("data-theme");
    paint();
    if (notify) window.dispatchEvent(new CustomEvent("themechange", { detail: { dark: isDark() } }));
  }
  function setTheme(t) { try { localStorage.setItem(KEY, t); } catch (e) { /* private mode: still applies for this page */ } apply(t, true); }
  apply(stored(), false);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () { if (!stored()) apply(null, true); });
  window.Shell = { isDark: isDark, setTheme: setTheme, paint: paint };

  // flat navigation: every page is one click away
  var NAV = [
    ["Top Bottom", "/top-bottom"], ["Options Simulator", "/simulator"], ["Strategy Builder", "/builder"],
    ["Backtesting", "/backtesting"], ["History", "/history"], ["Option Chain", "/options"],
    ["Settings", "/settings"],
  ];
  var ICON = {
    "/top-bottom": "M4 20V10M10 20V4M16 20v-7M22 20H2",
    "/simulator": "M4 4h16v16H4zM8 15l3-4 3 2 3-5",
    "/builder": "M6 4v16M18 4v16M6 8h12M6 16h12",
    "/backtesting": "M3 3v18h18M7 14l4-4 3 3 5-6",
    "/history": "M12 7v5l3 2M3 12a9 9 0 1 0 3-6.7M3 4v4h4",
    "/options": "M5 4h14v16H5zM9 9h6M9 13h6M9 17h3",
    "/settings": "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  };
  function svg(d, cls) { return '<svg class="' + (cls || "ic") + '" viewBox="0 0 24 24" aria-hidden="true"><path d="' + d + '"/></svg>'; }
  function fmt(v) { return v == null ? "–" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

  document.addEventListener("DOMContentLoaded", function () {
    var nav = document.querySelector("[data-shell]"); if (!nav) return;
    var here = location.pathname.replace(/\/+$/, "") || "/";
    if (here === "/journal") here = "/top-bottom";
    nav.className = "sb-nav";
    nav.innerHTML = NAV.map(function (n) {
      var on = n[1] === here;
      return '<a class="nav-link' + (on ? " active" : "") + '" href="' + n[1] + '"' + (on ? ' aria-current="page"' : "") + ">" + svg(ICON[n[1]]) + "<span>" + n[0] + "</span></a>";
    }).join("");

    // one sidebar for every page: the page's old sidebar / top bar is removed, its content stays
    var old = nav.closest(".sidebar") || nav.closest(".tnav");
    var sb = document.createElement("aside"); sb.className = "sb";
    sb.innerHTML = '<a class="sb-logo" href="/top-bottom"><i>' + svg("M4 20V10M10 20V4M16 20v-7M22 20H2", "ic w") + '</i><span>Algo Trading<small>Research · paper trading</small></span></a>';
    sb.appendChild(nav);
    var idx = document.createElement("div"); idx.className = "sb-idx";
    idx.innerHTML = ["NIFTY", "BANKNIFTY"].map(function (s) {
      return '<div class="iq" id="iq-' + s + '"><b>' + (s === "NIFTY" ? "NIFTY 50" : s) + '</b><span class="num p">–</span> <span class="num c"></span></div>';
    }).join("");
    sb.appendChild(idx);
    var foot = document.createElement("div"); foot.className = "sb-foot";
    foot.innerHTML = '<div class="seg" role="group" aria-label="Theme"><button class="seg-btn" data-theme-btn="light">Light</button><button class="seg-btn" data-theme-btn="dark">Dark</button></div>';
    sb.appendChild(foot);
    foot.querySelectorAll("[data-theme-btn]").forEach(function (b) { b.onclick = function () { setTheme(b.dataset.themeBtn); }; });
    if (old) old.remove();
    document.body.insertBefore(sb, document.body.firstChild);
    document.body.classList.add("has-sb");

    // thin status bar at the top of the content
    var gb = document.createElement("div"); gb.className = "gbar";
    gb.innerHTML = '<span class="gbar-title">' + (NAV.filter(function (n) { return n[1] === here; })[0] || [""])[0] + '</span><span class="sp"></span>'
      + '<span class="st" id="nav-mkt" title="NSE session 09:15-15:30 IST, Mon-Fri. Exchange holidays are not detected."><i></i><span>Market</span></span>'
      + '<span class="st paper" title="Paper trading and research only. No order is ever sent to a broker."><b>PAPER</b>&nbsp;no orders</span>';
    sb.parentNode.insertBefore(gb, sb.nextSibling);
    paint();

    var pollMkt = function () {
      fetch("/api/overview").then(function (r) { return r.ok ? r.json() : null; }).then(function (o) {
        var el = document.getElementById("nav-mkt"); if (!el) return;
        if (!o) { el.className = "st"; el.lastChild.textContent = "Market n/a"; return; }
        el.className = "st" + (o.market_open ? " open" : ""); el.lastChild.textContent = o.market_open ? "Market open" : "Market closed";
      }).catch(function () {});
    };
    var pollIdx = function () {
      ["NIFTY", "BANKNIFTY"].forEach(function (s) {
        fetch("/api/index/" + s).then(function (r) { return r.ok ? r.json() : null; }).then(function (q) {
          var el = document.getElementById("iq-" + s); if (!el) return;
          if (!q) { el.querySelector(".p").textContent = "n/a"; el.querySelector(".c").textContent = ""; return; }
          el.querySelector(".p").textContent = fmt(q.price);
          var c = el.querySelector(".c"); c.textContent = (q.pct >= 0 ? "+" : "") + q.pct.toFixed(2) + "%";
          c.className = "num c " + (q.change >= 0 ? "pos" : "neg");
          el.title = (q.live ? "Live tick" : "Last REST candle") + " · " + (q.change >= 0 ? "+" : "") + fmt(q.change) + " pts vs previous close";
        }).catch(function () {});
      });
    };
    pollMkt(); setInterval(pollMkt, 60000);
    pollIdx(); setInterval(pollIdx, 15000);
  });
})();

/* Sign-in session (password + TOTP login). Runs only when logged in under that login, which is when the readable algo_csrf cookie exists;
   with the old single-user login or no login nothing here does anything.
   - every same-origin POST/PUT/PATCH/DELETE gets the X-CSRF-Token header the server checks;
   - the first 401 from any /api/ call (heartbeat or a page's own poll) sends the page to the sign-in page, with the reason the server gives
     ("moved" = signed in on another device), so a tab that was signed out says so instead of silently showing empty data;
   - a heartbeat every 30 s tells the server this page is still open, and whether the person touched it (the 30-minute idle timeout);
   - a "Sign out" link in the sidebar. */
(function () {
  function cookie(n) { var m = document.cookie.match(new RegExp("(?:^|; )" + n + "=([^;]*)")); return m ? decodeURIComponent(m[1]) : null; }
  if (!cookie("algo_csrf")) return;
  if (/^\/(login|setup\/)/.test(location.pathname)) return;      // signed-out pages: never ping from here (a stale cookie would redirect in a loop)

  var leaving = false;
  function leave(reason) {
    if (leaving) return; leaving = true;
    location.href = "/login?e=" + (reason === "taken_over" ? "moved" : "ended");
  }

  var nativeFetch = window.fetch;
  window.fetch = function (input, init) {
    var api = false;
    try {
      var url = typeof input === "string" ? input : (input instanceof URL ? input.href : null);
      var u = url ? new URL(url, location.href) : null;
      var same = !!u && u.origin === location.origin;
      api = same && u.pathname.indexOf("/api/") === 0 && u.pathname !== "/api/auth/login";
      var method = ((init && init.method) || "GET").toUpperCase();
      if (same && method !== "GET" && method !== "HEAD") {
        var headers = new Headers((init && init.headers) || {});
        headers.set("X-CSRF-Token", cookie("algo_csrf") || "");
        init = Object.assign({}, init, { headers: headers });
      }
    } catch (e) { /* fall through to the plain call */ }
    var p = nativeFetch.call(this, input, init);
    if (api) {
      p.then(function (r) {
        if (r.status !== 401) return;
        r.clone().json().catch(function () { return {}; }).then(function (b) { leave(b && b.reason); });
      }).catch(function () { /* network error: not our business here */ });
    }
    return p;
  };

  var touched = false;
  ["mousemove", "mousedown", "keydown", "scroll", "touchstart"].forEach(function (ev) { addEventListener(ev, function () { touched = true; }, { passive: true }); });
  function beat() {
    var active = touched; touched = false;
    window.fetch("/api/auth/heartbeat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ active: active }) })
      .catch(function () { /* offline: try again at the next beat */ });
  }
  beat(); setInterval(beat, 30000);

  document.addEventListener("DOMContentLoaded", function () {
    var foot = document.querySelector(".sb-foot"); if (!foot) return;
    [["/account", "Account"], ["/logout", "Sign out"]].forEach(function (l) {
      var a = document.createElement("a"); a.href = l[0]; a.textContent = l[1]; a.className = "seg-btn";
      a.style.cssText = "display:block;margin-top:8px;text-align:center;text-decoration:none;border:1px solid var(--border-strong);border-radius:var(--radius-md)";
      foot.appendChild(a);
    });
  });
})();
