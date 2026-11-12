/* Mods: the catalogue on mods.html and one mod's page on mod.html.
   Data: mods.json (the index) and mods/<id>.json (one file per mod). Without this script both
   pages show a notice. */
(function () {
  "use strict";

  var PAGE_SIZE = 50;
  /* Sorting puts Kiln's own work first; the filter and the legend follow the reading order. */
  var ORDER = ["works", "may-work", "in-development", "planned", "doesnt-work", "no-plans", "not-checked"];
  var FILTER_ORDER = ["works", "may-work", "planned", "no-plans", "not-checked", "doesnt-work", "in-development"];
  var MARK = {
    "works": "mark-ok",
    "may-work": "mark-maybe",
    "planned": "mark-planned",
    "no-plans": "mark-none",
    "not-checked": "mark-unknown",
    "doesnt-work": "mark-bad",
    "in-development": "mark-dev"
  };

  var catalogue = document.querySelector("[data-mods-page]");
  var modPage = document.querySelector("[data-mod-page]");
  if (!catalogue && !modPage) return;

  function getJSON(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (response) {
      if (!response.ok) throw new Error(url + ": HTTP " + response.status);
      return response.json();
    });
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function label(data, status) {
    return (data.statuses && data.statuses[status]) || status;
  }

  function badge(text, status) {
    return el("span", "mark " + (MARK[status] || "mark-unknown"), text);
  }

  function rank(status) {
    var i = ORDER.indexOf(status);
    return i === -1 ? ORDER.length : i;
  }

  function formatDate(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    if (!m) return iso || "";
    var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
  }

  function reportsLine(r) {
    var parts = [];
    if (r.works) parts.push(r.works + " works");
    if (r.partly) parts.push(r.partly + " partly");
    if (r.broken) parts.push(r.broken + " doesn't work");
    return parts.length ? "Reports from players: " + parts.join(", ") + "." : "";
  }

  function modLink(m) {
    /* Every mod has a static page, mods/<id>.html; mod.html?id= still works for old links. */
    var a = el("a", null, m.name);
    a.href = "mods/" + encodeURIComponent(m.id) + ".html";
    return a;
  }

  /* mods.html: search, a filter by status, and a page of entries at a time. */
  /* The release layer is the default; with the switch on, a row shows what the development build does. */
  var devMode = false;
  function view(m) {
    if (!devMode || !m.next) return m;
    var v = {};
    for (var k in m) if (Object.prototype.hasOwnProperty.call(m, k)) v[k] = m[k];
    for (var j in m.next) if (Object.prototype.hasOwnProperty.call(m.next, j)) v[j] = m.next[j];
    ["run", "lacks", "stage", "works", "note", "data", "candidate"].forEach(function (k) { if (!(k in m.next)) delete v[k]; });
    if (m.note && !m.next.note) v.note = m.note;
    return v;
  }

  function renderRow(data, row) {
    var m = view(row);
    var li = el("li", "mod");
    var head = el("div", "mod-head");
    var name = el("h2", "mod-name");
    name.appendChild(modLink(m));
    head.appendChild(name);
    var note = m.note || (m.library ? "A library other mods depend on" : "");
    if (note) head.appendChild(el("p", "mod-note", note));
    var meta = el("div", "mod-meta");
    if (m.version) meta.appendChild(el("span", "mod-version", m.version));
    meta.appendChild(badge(label(data, m.status), m.status));
    if (!devMode && row.next && row.next.status !== row.status) {
      var next = badge("next: " + label(data, row.next.status), row.next.status);
      next.className += " mark-next";
      next.title = "In the current development build";
      meta.appendChild(next);
    }
    li.appendChild(head);
    li.appendChild(meta);
    var text = m.works || needsLine(m);
    var r = m.reports;
    if (r && (r.works || r.partly || r.broken)) text += (text ? " " : "") + reportsLine(r);
    li.appendChild(el("p", "mod-works", text));
    return li;
  }

  /* One line from the facts in an index row: what the scan found a mod lacks. */
  function needsLine(m) {
    if (m.status === "planned" && m.stage) return "Needs " + m.stage + "." + runNote(m);
    if (m.status === "no-plans") {
      var line = m.lacks ? "Lacks " + m.lacks + (m.lacks === 1 ? " thing" : " things") + " Kiln does not cover yet; not on the roadmap." : "Needs something that is not on the roadmap yet.";
      if (m.stage) line += " Also needs " + m.stage + ".";
      line += runNote(m);
      return line;
    }
    if (m.status === "may-work") {
      if (m.data) return "Data only: Kiln generates its structures in Horizon's worlds. Not tried in the game.";
      return "It ran on Kiln's test stand and nothing broke; everything it called is written in Kiln. Not tried in the game.";
    }
    if (m.candidate) return "Everything its code calls is covered by Kiln. Not run yet.";
    return runNote(m).trim();
  }

  function runNote(m) {
    if (m.run === "failed") return " It did not start on Kiln's test stand.";
    if (m.run === "needs-deps") return " It did not start without the mods it depends on.";
    if (m.run === "nested") return " It did not start: Kiln does not unpack the jars nested inside it yet.";
    return "";
  }

  function initCatalogue(data) {
    var list = catalogue.querySelector("[data-mods-list]");
    var results = catalogue.querySelector("[data-mods-results]");
    var more = catalogue.querySelector("[data-mods-more]");
    var toolbar = catalogue.querySelector("[data-mods-toolbar]");
    var input = catalogue.querySelector("#mods-search");
    var filter = catalogue.querySelector("[data-mods-filter]");

    var devSwitch = catalogue.querySelector("[data-mods-dev]");
    var build = (data.development && data.development.build) || "";
    if (devSwitch && !build) devSwitch.closest("p").hidden = true;
    function statusOf(m) { return view(m).status; }
    function sorted() {
      return data.mods.slice().sort(function (a, b) {
        return (rank(statusOf(a)) - rank(statusOf(b))) ||
          String(a.name).localeCompare(String(b.name), "en", { sensitivity: "base" });
      });
    }
    var mods = sorted();
    var counts = {};
    var countNodes = {};
    function recount() {
      counts = {};
      mods.forEach(function (m) { var st = statusOf(m); counts[st] = (counts[st] || 0) + 1; });
      Object.keys(countNodes).forEach(function (st) { countNodes[st].textContent = String(st === "all" ? mods.length : (counts[st] || 0)); });
    }
    recount();

    /* The filter comes from the data: "All" and every status the index knows. */
    var known = Object.keys(data.statuses || {});
    var statuses = ["all"].concat(FILTER_ORDER.filter(function (s) { return known.indexOf(s) !== -1; }));
    known.forEach(function (s) { if (statuses.indexOf(s) === -1) statuses.push(s); });
    var radios = [];
    statuses.forEach(function (s) {
      var radio = el("input", "visually-hidden");
      radio.type = "radio";
      radio.name = "status";
      radio.id = "status-" + s;
      radio.value = s;
      var lab = el("label", null, s === "all" ? "All" : label(data, s));
      lab.htmlFor = radio.id;
      lab.appendChild(document.createTextNode(" "));
      var count = el("span", "count", String(s === "all" ? mods.length : (counts[s] || 0)));
      countNodes[s] = count;
      lab.appendChild(count);
      filter.appendChild(radio);
      filter.appendChild(lab);
      radios.push(radio);
    });

    var meta = catalogue.querySelector("[data-mods-meta]");
    function showMeta() {
      if (!meta || !data.updated) return;
      meta.textContent = devMode && build
        ? "Catalogue as of " + formatDate(data.updated) + ", counted for the " + build + "."
        : "Catalogue as of " + formatDate(data.updated) + (data.kiln ? ", checked against Kiln " + data.kiln : "") + ".";
    }
    showMeta();

    var state = { query: "", status: "all", shown: PAGE_SIZE };
    try {
      var params = new URLSearchParams(window.location.search);
      state.query = (params.get("q") || "").trim();
      state.status = params.get("status") || "all";
      devMode = !!build && params.get("build") === "dev";
    } catch (e) { /* no URL state */ }
    if (devSwitch) devSwitch.checked = devMode;
    if (devMode) { mods = sorted(); recount(); showMeta(); }
    if (statuses.indexOf(state.status) === -1) state.status = "all";
    input.value = state.query;
    radios.forEach(function (r) { r.checked = r.value === state.status; });
    toolbar.hidden = false;
    toolbar.addEventListener("submit", function (event) { event.preventDefault(); });

    function matching() {
      var q = state.query.toLowerCase();
      return mods.filter(function (m) {
        return (state.status === "all" || statusOf(m) === state.status) &&
          (!q || String(m.name).toLowerCase().indexOf(q) !== -1);
      });
    }

    function clear() {
      state.query = "";
      state.status = "all";
      state.shown = PAGE_SIZE;
      input.value = "";
      radios.forEach(function (r) { r.checked = r.value === "all"; });
      render();
      input.focus();
    }

    function render() {
      var all = matching();
      var shown = all.slice(0, state.shown);
      var fragment = document.createDocumentFragment();
      if (!all.length) {
        var empty = el("li", "mod-empty");
        empty.appendChild(el("p", null, state.query
          ? "No mods match “" + state.query + "” with this filter."
          : "No mods with this status yet."));
        var reset = el("button", "btn-quiet", "Clear search and filter");
        reset.type = "button";
        reset.addEventListener("click", clear);
        empty.appendChild(reset);
        fragment.appendChild(empty);
      }
      shown.forEach(function (m) { fragment.appendChild(renderRow(data, m)); });
      list.replaceChildren(fragment);
      results.textContent = !all.length ? "No mods match."
        : shown.length < all.length ? "Showing " + shown.length + " of " + all.length + " mods"
        : all.length + (all.length === 1 ? " mod" : " mods");
      more.hidden = shown.length >= all.length;
      try {
        var p = new URLSearchParams();
        if (state.query) p.set("q", state.query);
        if (state.status !== "all") p.set("status", state.status);
        if (devMode) p.set("build", "dev");
        var qs = p.toString();
        window.history.replaceState(null, "", window.location.pathname + (qs ? "?" + qs : "") + window.location.hash);
      } catch (e) { /* no URL state */ }
    }

    function onSearch() {
      state.query = input.value.trim();
      state.shown = PAGE_SIZE;
      render();
    }
    input.addEventListener("input", onSearch);
    /* Safari's clear button on a search field fires "search". */
    input.addEventListener("search", onSearch);
    /* Coming back to the page (the back button, a page the browser kept): filter by what the search field and the
       status buttons show, so a search can never apply while its field looks empty. */
    window.addEventListener("pageshow", function () {
      var checked = radios.filter(function (r) { return r.checked; })[0];
      var status = checked ? checked.value : state.status;
      if (input.value.trim() === state.query && status === state.status) return;
      state.query = input.value.trim();
      state.status = status;
      state.shown = PAGE_SIZE;
      render();
    });
    radios.forEach(function (r) {
      r.addEventListener("change", function () {
        if (!r.checked) return;
        state.status = r.value;
        state.shown = PAGE_SIZE;
        render();
      });
    });
    if (devSwitch) {
      devSwitch.addEventListener("change", function () {
        devMode = devSwitch.checked;
        mods = sorted();
        recount();
        showMeta();
        state.shown = PAGE_SIZE;
        render();
      });
    }
    more.addEventListener("click", function () {
      var first = state.shown;
      state.shown += PAGE_SIZE;
      render();
      var next = list.children[first];
      if (next) {
        var target = next.querySelector("a") || next;
        if (target === next) target.tabIndex = -1;
        target.focus();
      }
    });

    render();
  }

  /* mod.html: one mod, from mods/<id>.json. */
  function showMissing(content, id) {
    var box = el("div", "mod-empty");
    box.appendChild(el("p", null, id
      ? "There is no mod with the id “" + id + "” in the catalogue."
      : "No mod was named. Pick one from the list."));
    var a = el("a", "btn-quiet", "All mods");
    a.href = "mods.html";
    box.appendChild(a);
    content.replaceChildren(box);
  }

  function renderMod(content, m) {
    document.title = m.name + " — Kiln mods";
    var fragment = document.createDocumentFragment();

    var head = el("div", "sheet-head");
    var status = el("p", "mod-page-status");
    status.appendChild(badge(m.statusLabel || m.status, m.status));
    head.appendChild(status);
    var facts = el("dl", "mod-facts");
    function fact(term, value) {
      var group = el("div");
      group.appendChild(el("dt", null, term));
      var dd = el("dd");
      if (typeof value === "string") dd.textContent = value; else dd.appendChild(value);
      group.appendChild(dd);
      facts.appendChild(group);
    }
    if (m.version) fact("Version", m.version);
    if (m.author) fact("Author", m.author);
    if (m.library) fact("Kind", "A library other mods depend on");
    if (m.checked && m.checked.kiln) {
      fact("Checked with", "Kiln " + m.checked.kiln +
        (m.checked.commit ? " (" + m.checked.commit + ")" : "") +
        (m.checked.date ? ", " + formatDate(m.checked.date) : "") +
        (m.checked.horizon ? ", on Horizon " + m.checked.horizon : "") +
        (m.checked.by ? ", by " + m.checked.by : ""));
    }
    if (m.url) {
      var link = el("a", null, String(m.url).replace(/^https?:\/\//, ""));
      link.href = m.url;
      link.rel = "noopener";
      fact("Page", link);
    }
    if (m.note) fact("Source", m.note);
    if (facts.childNodes.length) head.appendChild(facts);
    fragment.appendChild(head);

    var body = el("div", "sheet-body");
    body.appendChild(el("h1", "page-title", m.name));
    if (m.statusMeaning) body.appendChild(el("p", "mod-page-meaning", m.statusMeaning));

    function section(title, nodes) {
      var s = el("section", "mod-section");
      s.appendChild(el("h2", null, title));
      nodes.forEach(function (n) { s.appendChild(n); });
      body.appendChild(s);
    }
    if (m.works) section(m.status === "in-development" ? "What it will do" : "What works", [el("p", null, m.works)]);
    if (m.evidence) section("Evidence", [el("p", null, m.evidence)]);
    if (m.needs && m.needs.length) {
      var ul = el("ul", "needs-list");
      m.needs.forEach(function (n) { ul.appendChild(el("li", null, n)); });
      var nodes = [ul];
      if (m.roadmap) nodes.push(el("p", null, "On the roadmap: " + m.roadmap));
      section("What it needs from Kiln", nodes);
    }
    if (m.description) section("In the author's words", [el("p", "mod-quote", m.description)]);
    if (m.scanned && m.scanned.date) {
      section("Scan", [el("p", null, "Scanned on " + formatDate(m.scanned.date) +
        (m.scanned.kiln ? " against Kiln " + m.scanned.kiln : "") +
        (m.scanned.file ? ", file " + m.scanned.file : "") + ". The scan reads the mod's classes; it does not run them.")]);
    }

    var dev = m.development;
    var passedIn = function (x) { return !!(x && x.run && x.run.passed); };
    if (dev && (dev.status !== m.status || passedIn(dev) !== passedIn(m))) {
      var devNodes = [];
      var lead = el("p");
      lead.appendChild(badge(dev.statusLabel || dev.status, dev.status));
      lead.appendChild(document.createTextNode(" in the " + (dev.build || "development build") + "."));
      devNodes.push(lead);
      if (dev.statusMeaning) devNodes.push(el("p", null, dev.statusMeaning));
      if (dev.works) devNodes.push(el("p", null, dev.works));
      if (dev.checked && dev.checked.kiln) {
        devNodes.push(el("p", null, "Checked with Kiln " + dev.checked.kiln +
          (dev.checked.commit ? " (" + dev.checked.commit + ")" : "") +
          (dev.checked.date ? ", " + formatDate(dev.checked.date) : "") +
          (dev.checked.horizon ? ", on Horizon " + dev.checked.horizon : "") + "."));
      }
      if (dev.evidence) devNodes.push(el("p", null, dev.evidence));
      section("In the development builds", devNodes);
    }
    /* Reports live on the mod's own page, which reads them from the server and has the form. */
    var reportLine = el("p");
    var reportLink = el("a", null, "Reports from players and the form");
    reportLink.href = "mods/" + encodeURIComponent(m.id) + ".html#reports";
    reportLine.appendChild(reportLink);
    reportLine.appendChild(document.createTextNode(" are on the mod's own page."));
    section("Reports from players", [reportLine]);
    fragment.appendChild(body);

    content.replaceChildren(fragment);
  }

  function initModPage() {
    var content = modPage.querySelector("[data-mod-content]");
    var id = "";
    try { id = new URLSearchParams(window.location.search).get("id") || ""; } catch (e) { /* no id */ }
    if (!/^[a-z0-9][a-z0-9-]{0,80}$/.test(id)) { showMissing(content, id); return; }
    getJSON("mods/" + id + ".json").then(function (m) {
      renderMod(content, m);
    }).catch(function (error) {
      showMissing(content, id);
      if (window.console) console.warn(error);
    });
  }

  if (modPage) initModPage();

  if (catalogue) {
    getJSON("mods.json").then(function (data) {
      if (!data || !Array.isArray(data.mods)) throw new Error("mods.json: no mods array");
      initCatalogue(data);
    }).catch(function (error) {
      if (catalogue) {
        var list = catalogue.querySelector("[data-mods-list]");
        var notice = el("li", "mod-empty");
        var p = el("p", null, "The list of mods could not be loaded. Reload the page, or open ");
        var link = el("a", null, "mods.json");
        link.href = "mods.json";
        p.appendChild(link);
        p.appendChild(document.createTextNode(" directly."));
        notice.appendChild(p);
        list.replaceChildren(notice);
      }
      if (window.console) console.warn(error);
    });
  }
})();
