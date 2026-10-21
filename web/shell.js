/* Shared shell: theme (persistent, system-aware) + sidebar navigation. Loaded in <head> so the theme is applied before first paint. */
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
  document.addEventListener("DOMContentLoaded", function () {
    var nav = document.querySelector("[data-shell]"); if (!nav) return;
    var here = location.pathname.replace(/\/+$/, "") || "/";
    if (here === "/journal") here = "/top-bottom";
    nav.innerHTML = NAV.map(function (n) {
      return '<a class="nav-link ' + (n[1] === here ? "active" : "") + '" href="' + n[1] + '"' + (n[1] === here ? ' aria-current="page"' : "") + ">" + n[0] + "</a>";
    }).join("");
    var side = nav.closest(".sidebar");
    // every page gets the same top navigation bar: an old sidebar is replaced by it
    if (side) {
      var bar = document.createElement("header"); bar.className = "tnav";
      bar.innerHTML = '<a class="logo" href="/top-bottom"><i><b style="height:8px"></b><b style="height:13px"></b><b style="height:20px"></b><b style="height:16px"></b></i><span>Algo Trading</span></a>';
      bar.appendChild(nav);
      document.body.insertBefore(bar, document.body.firstChild);
      side.remove();
    }
    var tbar = nav.closest(".tnav");
    if (tbar) {
      document.body.classList.add("has-tnav");
      var st = document.createElement("div"); st.className = "nav-status";
      st.innerHTML = '<span class="st" id="nav-mkt" title="NSE session 09:15-15:30 IST, Mon-Fri. Exchange holidays are not detected."><i></i><span>Market</span></span><span class="st paper" title="Paper trading and research only. No order is ever sent to a broker."><b>PAPER</b>&nbsp;no orders</span>';
      tbar.appendChild(st);
      var pollMkt = function () {
        fetch("/api/overview").then(function (r) { return r.ok ? r.json() : null; }).then(function (o) {
          var el = document.getElementById("nav-mkt"); if (!el) return;
          if (!o) { el.className = "st"; el.lastChild.textContent = "Market n/a"; return; }
          el.className = "st" + (o.market_open ? " open" : ""); el.lastChild.textContent = o.market_open ? "Market open" : "Market closed";
        }).catch(function () {});
      };
      pollMkt(); setInterval(pollMkt, 60000);
      if (!document.getElementById("theme") && !tbar.querySelector(".themeb")) {   // pages with their own toggle (Dashboard) keep it
        var tb = document.createElement("button"); tb.className = "themeb"; tb.setAttribute("aria-label", "Toggle theme"); tb.title = "Toggle light / dark theme";
        var icon = function () { tb.textContent = isDark() ? "☀" : "☾"; };
        tb.onclick = function () { setTheme(isDark() ? "light" : "dark"); icon(); };
        window.addEventListener("themechange", icon); icon();
        tbar.appendChild(tb);
      }
      return;
    }
    if (!side) return;
    if (!document.getElementById("theme")) {   // pages with their own toggle (Dashboard) keep it
      var box = document.createElement("div"); box.className = "side-foot";
      box.innerHTML = '<div class="seg" role="group" aria-label="Theme"><button class="seg-btn" data-theme-btn="light">Light</button><button class="seg-btn" data-theme-btn="dark">Dark</button></div>';
      side.appendChild(box);
      box.querySelectorAll("[data-theme-btn]").forEach(function (b) { b.onclick = function () { setTheme(b.dataset.themeBtn); }; });
    }
    var c = document.createElement("button"); c.className = "side-collapse"; c.setAttribute("aria-label", "Collapse sidebar"); c.textContent = "«";
    side.appendChild(c);
    var app = side.closest(".app");
    function col(on) { app.classList.toggle("collapsed", on); c.textContent = on ? "»" : "«"; try { localStorage.setItem("sidebar", on ? "1" : "0"); } catch (e) {} setTimeout(function () { window.dispatchEvent(new Event("resize")); }, 200); }
    c.onclick = function () { col(!app.classList.contains("collapsed")); };
    var s = null; try { s = localStorage.getItem("sidebar"); } catch (e) {}
    if (s === "1") col(true);
    paint();
  });
})();
