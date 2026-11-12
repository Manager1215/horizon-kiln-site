/* Reports from players on a mod's page: the counts and the reports from the site's own server, and the form for a
   reader who is signed in. Without this script the section links to /reports/<id>, a plain page with the same
   reports and the same form. The account comes from site.js (window.kilnAccount), one request for the whole page. */
(function () {
  "use strict";
  var box = document.querySelector("[data-reports]");
  if (!box || !window.fetch) return;
  var id = box.getAttribute("data-reports");
  if (!/^[a-z0-9][a-z0-9-]{0,80}$/.test(id)) return;
  var line = box.querySelector("[data-reports-line]");
  var list = box.querySelector("[data-reports-list]");
  var signin = box.querySelector("[data-report-signin]");
  var form = box.querySelector("[data-report-form]");
  var status = box.querySelector("[data-report-status]");
  var removeButton = box.querySelector("[data-report-delete]");
  var MARK = { works: "mark-ok", partly: "mark-maybe", broken: "mark-bad" };
  var state = { summary: null, me: null };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function longDate(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    if (!m) return iso || "";
    return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
  }

  function countsLine(s) {
    var total = (s.works || 0) + (s.partly || 0) + (s.broken || 0);
    if (!total) return "No reports from players yet.";
    var parts = [];
    if (s.works) parts.push(s.works + (s.works === 1 ? " says it works" : " say it works"));
    if (s.partly) parts.push(s.partly + " partly");
    if (s.broken) parts.push(s.broken + (s.broken === 1 ? " says it doesn't" : " say it doesn't"));
    return (total === 1 ? "One report: " : total + " reports: ") + parts.join(", ") + ".";
  }

  function showSummary(s) {
    state.summary = s;
    line.textContent = countsLine(s);
    if ((s.total || 0) > (s.latest || []).length) {
      line.appendChild(document.createTextNode(" "));
      var all = el("a", null, "All of them");
      all.href = "/reports/" + id;
      line.appendChild(all);
      line.appendChild(document.createTextNode("."));
    }
    list.replaceChildren();
    (s.latest || []).forEach(function (r) {
      var li = el("li", "report");
      var head = el("p", "report-head");
      head.appendChild(el("span", "mark " + (MARK[r.result] || "mark-unknown"), r.resultLabel || r.result));
      head.appendChild(document.createTextNode(" "));
      head.appendChild(el("span", "report-name", r.name));
      if (r.team) head.appendChild(el("span", "mark mark-team", "Kiln team"));
      li.appendChild(head);
      if (r.text) li.appendChild(el("p", "report-text", r.text));
      var meta = [r.buildLabel];
      if (r.version) meta.push("mod " + r.version);
      meta.push(longDate(r.date));
      li.appendChild(el("p", "report-meta", meta.join(" · ")));
      list.appendChild(li);
    });
    list.hidden = !(s.latest && s.latest.length);
    if (form) {
      var select = form.elements.channel;
      (s.builds || []).forEach(function (b) {
        for (var i = 0; i < select.options.length; i++) {
          if (select.options[i].value === b.value) select.options[i].textContent = b.label;
        }
      });
      for (var i = select.options.length - 1; i >= 0; i--) {
        var known = (s.builds || []).some(function (b) { return b.value === select.options[i].value; });
        if (!known) select.remove(i);
      }
    }
  }

  function buildFor(channel) {
    var builds = (state.summary && state.summary.builds) || [];
    for (var i = 0; i < builds.length; i++) if (builds[i].value === channel) return builds[i].build;
    return "";
  }

  function mineFor(channel) {
    var build = buildFor(channel);
    var mine = (state.me && state.me.mine) || [];
    for (var i = 0; i < mine.length; i++) if (mine[i].build === build) return mine[i];
    return null;
  }

  /* The form shows the reader's own report for the chosen build, if there is one: sending replaces it. */
  function fillForm() {
    var mine = mineFor(form.elements.channel.value);
    var radios = form.querySelectorAll("input[name=result]");
    for (var i = 0; i < radios.length; i++) radios[i].checked = !!mine && radios[i].value === mine.result;
    form.elements.version.value = mine ? mine.version : ((state.summary && state.summary.version) || form.elements.version.defaultValue);
    form.elements.text.value = mine ? mine.text : "";
    form.querySelector(".report-send").textContent = mine ? "Save the change" : "Send the report";
    removeButton.hidden = !mine;
  }

  function showAccount(me) {
    state.me = me && me.signedIn ? me : null;
    if (!state.me) {
      signin.hidden = false;
      form.hidden = true;
      return;
    }
    signin.hidden = true;
    if (!form.elements.channel.options.length) {
      form.hidden = true;
      return;
    }
    form.elements.csrf.value = state.me.csrf;
    form.hidden = false;
    fillForm();
  }

  function send(path, body) {
    return fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": state.me ? state.me.csrf : "" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (r.status === 401) { showAccount(null); throw new Error("You are signed out. Sign in again to send the report."); }
        if (!r.ok) throw new Error(data.error || "The server did not take it. Reload the page and try again.");
        return data;
      });
    }).then(function (data) {
      state.me.mine = data.mine || [];
      showSummary(data.summary);
      fillForm();
      return data;
    });
  }

  var summaryRequest = fetch("/reports/" + id + ".json", { headers: { Accept: "application/json" } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .catch(function () { return null; });
  var accountRequest = window.kilnAccount || fetch("/account/me.json?mod=" + id, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .catch(function () { return null; });

  Promise.all([summaryRequest, accountRequest]).then(function (both) {
    if (!both[0]) return; /* the server is out of reach: the link to the reports page stays */
    showSummary(both[0]);
    if (form && signin) showAccount(both[1]);
  });

  if (!form) return;
  form.elements.channel.addEventListener("change", function () {
    status.textContent = "";
    fillForm();
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (!state.me) return;
    var data = new FormData(form);
    status.textContent = "Sending.";
    send("/reports/" + id, { result: data.get("result"), channel: data.get("channel"), version: data.get("version") || "", text: data.get("text") || "" })
      .then(function () { status.textContent = "Saved. Thank you."; })
      .catch(function (error) { status.textContent = error.message; });
  });

  removeButton.addEventListener("click", function () {
    var mine = mineFor(form.elements.channel.value);
    if (!mine || !state.me) return;
    status.textContent = "Deleting.";
    send("/reports/" + id + "/delete", { n: mine.n })
      .then(function () {
        status.textContent = "Your report for this build is deleted.";
        form.querySelector(".report-send").focus();
      })
      .catch(function (error) { status.textContent = error.message; });
  });
})();
