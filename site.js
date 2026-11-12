/* Progressive enhancements. The page works without this file. */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* The account link in the header: the name once signed in. One request to the site's own server, which a mod
     page's report form (reports.js) waits on too; if it fails, the link to the sign-in page stays. */
  var accountLink = document.querySelector("[data-account-link]");
  if (accountLink && window.fetch && /^https?:$/.test(window.location.protocol)) {
    var reports = document.querySelector("[data-reports]");
    var modId = reports ? reports.getAttribute("data-reports") : "";
    window.kilnAccount = fetch("/account/me.json" + (/^[a-z0-9][a-z0-9-]{0,80}$/.test(modId || "") ? "?mod=" + modId : ""), {
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    }).then(function (response) {
      return response.ok ? response.json() : null;
    }).catch(function () { return null; });
    window.kilnAccount.then(function (me) {
      if (me && me.signedIn && me.name) {
        accountLink.textContent = me.name;
        accountLink.href = "/account/";
      }
    });
  }

  /* What's new: refresh the releases from changelog.json; the static entry stays if that fails. */
  var news = document.querySelector("[data-changelog]");
  if (news && window.fetch) {
    var make = function (tag, className, text) {
      var node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = text;
      return node;
    };
    var longDate = function (iso) {
      var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
      if (!m) return iso || "";
      return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
        .toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
    };
    fetch("changelog.json", { headers: { Accept: "application/json" } }).then(function (response) {
      if (!response.ok) throw new Error("changelog.json: HTTP " + response.status);
      return response.json();
    }).then(function (data) {
      if (!data || !Array.isArray(data.releases) || !data.releases.length) return;
      var latest = data.releases[0];
      var mark = document.querySelector("[data-latest-mark]");
      var note = document.querySelector("[data-latest-note]");
      if (latest.version && mark) {
        var stage = /-(alpha|beta)/.exec(String(latest.version));
        mark.textContent = (stage ? stage[1].charAt(0).toUpperCase() + stage[1].slice(1) : "Release") + " " +
          String(latest.version).replace(/-(alpha|beta).*$/, "");
      }
      if (latest.version && note) note.textContent = "Latest public release " + latest.version + (latest.date ? ", " + longDate(latest.date) : "") + ".";
      var isMajor = function (version) {
        var m = /(\d+)\.(\d+)(?:\.(\d+))?/.exec(String(version || ""));
        return !m || !m[3] || m[3] === "0";
      };
      var firstSentence = function (text) {
        var m = /(.+?[.!?])(\s|$)/.exec(String(text || ""));
        return m ? m[1] : String(text || "");
      };
      var itemList = function (items) {
        var ul = make("ul", "release-items");
        items.forEach(function (item) { ul.appendChild(make("li", null, item)); });
        return ul;
      };
      var majorIndex = -1;
      data.releases.some(function (release, i) { if (isMajor(release.version)) { majorIndex = i; return true; } return false; });
      if (majorIndex === -1) majorIndex = 0;
      var major = data.releases[majorIndex];
      var others = data.releases.filter(function (_release, i) { return i !== majorIndex; }).slice(0, 8);
      var items = major.items || [];
      var fragment = document.createDocumentFragment();
      var li = make("li", "release");
      var head = make("div", "release-head");
      head.appendChild(make("p", "release-version", major.version || ""));
      if (major.date) {
        var date = make("p", "release-date");
        var time = make("time", null, longDate(major.date));
        time.dateTime = major.date;
        date.appendChild(time);
        head.appendChild(date);
      }
      li.appendChild(head);
      var body = make("div", "release-body");
      if (major.summary) body.appendChild(make("p", "release-summary", firstSentence(major.summary)));
      var details = make("details", "release-more");
      var summary = make("summary");
      summary.appendChild(make("span", "when-closed", "Show more"));
      summary.appendChild(make("span", "when-open", "Show less"));
      details.appendChild(summary);
      var more = make("div", "release-more-body");
      if (items.length) more.appendChild(itemList(items));
      if (major.url) {
        var links = make("p", "release-links");
        var a = make("a", null, "Release notes");
        a.href = major.url;
        links.appendChild(a);
        more.appendChild(links);
      }
      if (others.length) {
        more.appendChild(make("p", "release-others-title", "Smaller releases"));
        var list = make("ul", "release-small-list");
        others.forEach(function (release) {
          var row = make("li");
          row.appendChild(make("span", "release-small-version", release.version || ""));
          row.appendChild(document.createTextNode(" "));
          if (release.date) {
            var when = make("time", "release-small-date", longDate(release.date));
            when.dateTime = release.date;
            row.appendChild(when);
          }
          row.appendChild(make("br"));
          row.appendChild(document.createTextNode(firstSentence(release.summary) + " "));
          if (release.url) {
            var link = make("a", null, "Release notes");
            link.href = release.url;
            row.appendChild(link);
          }
          list.appendChild(row);
        });
        more.appendChild(list);
      }
      details.appendChild(more);
      body.appendChild(details);
      li.appendChild(body);
      fragment.appendChild(li);
      news.replaceChildren(fragment);
    }).catch(function (error) { if (window.console) console.warn(error); });
  }

  /* Reading progress in the header, where CSS scroll-driven animations are missing. */
  var heat = document.querySelector(".heat-line");
  if (heat && !(window.CSS && CSS.supports && CSS.supports("animation-timeline", "scroll()"))) {
    var waiting = false;
    var updateHeat = function () {
      var max = document.documentElement.scrollHeight - window.innerHeight;
      var progress = max > 0 ? Math.min(1, Math.max(0, window.scrollY / max)) : 0;
      heat.style.setProperty("--scroll-progress", progress.toFixed(4));
      waiting = false;
    };
    window.addEventListener("scroll", function () {
      if (!waiting) { waiting = true; requestAnimationFrame(updateHeat); }
    }, { passive: true });
    window.addEventListener("resize", updateHeat);
    updateHeat();
  }

  /* FAQ: open and close smoothly where CSS cannot interpolate to auto height. */
  var cssCanAnimate = window.CSS && CSS.supports && CSS.supports("interpolate-size", "allow-keywords");
  if (!cssCanAnimate && !reduceMotion.matches && "animate" in Element.prototype) {
    document.querySelectorAll("details.faq, details.release-more").forEach(function (details) {
      var summary = details.querySelector("summary");
      if (!summary) return;
      var running = null;
      summary.addEventListener("click", function (event) {
        event.preventDefault();
        var from = details.offsetHeight;
        if (running) running.cancel();
        var closing = details.open && !details.hasAttribute("data-closing");
        details.style.overflow = "hidden";
        details.style.height = from + "px";
        var to;
        if (closing) {
          details.setAttribute("data-closing", "");
          to = summary.offsetHeight + (details.offsetHeight - details.clientHeight);
        } else {
          details.removeAttribute("data-closing");
          details.open = true;
          to = details.scrollHeight;
        }
        running = details.animate(
          [{ height: from + "px" }, { height: to + "px" }],
          { duration: 280, easing: "cubic-bezier(.2, .7, .2, 1)" }
        );
        running.onfinish = function () {
          running = null;
          details.style.height = "";
          details.style.overflow = "";
          if (details.hasAttribute("data-closing")) {
            details.removeAttribute("data-closing");
            details.open = false;
          }
        };
        running.oncancel = function () { running = null; };
      });
    });
  }
})();
