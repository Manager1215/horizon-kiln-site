#!/usr/bin/env python3
"""Writes one static HTML page per mod, mods/<id>.html, from mods.json and mods/<id>.json, plus sitemap.xml.

    python3 .github/scripts/build-pages.py [--out .]

mod.html draws the same page with a script from the same files; the static pages exist so that search engines
and readers without JavaScript get every mod's page as a page of its own. The markup mirrors what mods.js builds,
so style.css styles both. Pages are deterministic: the same data gives the same bytes.
"""
import argparse
import datetime
import html
import json
import os
import re

SITE = "https://horizon-kiln.cc"
MARK = {
    "works": "mark-ok",
    "may-work": "mark-maybe",
    "planned": "mark-planned",
    "no-plans": "mark-none",
    "not-checked": "mark-unknown",
    "doesnt-work": "mark-bad",
    "in-development": "mark-dev",
}
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def esc(text):
    return html.escape(str(text if text is not None else ""), quote=True)


def long_date(iso):
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", iso or "")
    if not m:
        return iso or ""
    d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return "%d %s %d" % (d.day, d.strftime("%B"), d.year)


def description_of(m):
    """The meta description: the status, then the evidence or the status's meaning, within 160 characters."""
    label = m.get("statusLabel") or m.get("status") or ""
    lead = "%s%s in Horizon through Kiln: %s." % (m.get("name", ""), (" " + m["version"]) if m.get("version") else "", label)
    rest = m.get("works") or m.get("evidence") or m.get("statusMeaning") or ""
    text = (lead + " " + rest).strip()
    if len(text) > 160:
        text = text[:157].rsplit(" ", 1)[0] + "…"
    return text


def facts_of(m):
    rows = []
    if m.get("version"):
        rows.append(("Version", esc(m["version"])))
    if m.get("author"):
        rows.append(("Author", esc(m["author"])))
    if m.get("library"):
        rows.append(("Kind", "A library other mods depend on"))
    checked = m.get("checked") or {}
    if checked.get("kiln"):
        rows.append(("Checked with", esc("Kiln " + str(checked["kiln"]) +
                                         (" (" + str(checked["commit"]) + ")" if checked.get("commit") else "") +
                                         (", " + long_date(checked["date"]) if checked.get("date") else "") +
                                         (", on Horizon " + str(checked["horizon"]) if checked.get("horizon") else "") +
                                         (", by " + str(checked["by"]) if checked.get("by") else ""))))
    if m.get("url"):
        shown = re.sub(r"^https?://", "", str(m["url"]))
        rows.append(("Page", '<a href="%s" rel="noopener">%s</a>' % (esc(m["url"]), esc(shown))))
    if m.get("note"):
        rows.append(("Source", esc(m["note"])))
    return rows


def sections_of(m):
    out = []
    if m.get("works"):
        out.append(("What it will do" if m.get("status") == "in-development" else "What works", "<p>%s</p>" % esc(m["works"])))
    if m.get("evidence"):
        out.append(("Evidence", "<p>%s</p>" % esc(m["evidence"])))
    needs = m.get("needs") or []
    if needs:
        body = '<ul class="needs-list">' + "".join("<li>%s</li>" % esc(n) for n in needs) + "</ul>"
        if m.get("roadmap"):
            body += "<p>On the roadmap: %s</p>" % esc(m["roadmap"])
        out.append(("What it needs from Kiln", body))
    if m.get("description"):
        out.append(("In the author's words", '<p class="mod-quote">%s</p>' % esc(m["description"])))
    scanned = m.get("scanned") or {}
    if scanned.get("date"):
        out.append(("Scan", "<p>%s</p>" % esc("Scanned on " + long_date(scanned["date"]) +
                                             (" against Kiln " + str(scanned["kiln"]) if scanned.get("kiln") else "") +
                                             (", file " + str(scanned["file"]) if scanned.get("file") else "") +
                                             ". The scan reads the mod's classes; it does not run them.")))
    return out


def development_section(m):
    """What main says about the mod, when it differs from the release: the status, the evidence, the needs."""
    dev = m.get("development") or {}
    if not dev:
        return ""
    passed = lambda x: bool(((x or {}).get("run") or {}).get("passed"))
    same = dev.get("status") == m.get("status") and passed(dev) == passed(m)
    if same:
        return ""
    build = dev.get("build") or "main"
    parts = ['<p><span class="mark %s">%s</span> in the %s.</p>' % (MARK.get(dev.get("status"), "mark-unknown"),
                                                                      esc(dev.get("statusLabel") or dev.get("status") or ""), esc(build))]
    if dev.get("statusMeaning"):
        parts.append("<p>%s</p>" % esc(dev["statusMeaning"]))
    if dev.get("works"):
        parts.append("<p>%s</p>" % esc(dev["works"]))
    checked = dev.get("checked") or {}
    if checked.get("kiln"):
        parts.append("<p>%s</p>" % esc("Checked with Kiln " + str(checked["kiln"]) +
                                       (" (" + str(checked["commit"]) + ")" if checked.get("commit") else "") +
                                       (", " + long_date(checked["date"]) if checked.get("date") else "") +
                                       (", on Horizon " + str(checked["horizon"]) if checked.get("horizon") else "") + "."))
    if dev.get("evidence"):
        parts.append("<p>%s</p>" % esc(dev["evidence"]))
    needs = dev.get("needs") or []
    if needs and needs != (m.get("needs") or []):
        parts.append('<ul class="needs-list">' + "".join("<li>%s</li>" % esc(n) for n in needs) + "</ul>")
        if dev.get("roadmap"):
            parts.append("<p>On the roadmap: %s</p>" % esc(dev["roadmap"]))
    parts.append('<p class="dev-note">Development builds are main as it is today, not a release: see the <a href="../index.html#faq-dev">FAQ</a>.</p>')
    return '      <section class="mod-section development">\n        <h2>In the development builds</h2>\n        ' + "\n        ".join(parts) + "\n      </section>\n"


REPORTS = """      <section class="mod-section reports" id="reports" data-reports="{id}">
        <h2>Reports from players</h2>
        <p data-reports-line><a href="/reports/{id}">Read the reports from players</a>, or write your own.</p>
        <ul class="reports-list" data-reports-list hidden></ul>
        <p class="report-signin" data-report-signin hidden><a href="/account/signin?next=/mods/{id}.html%23reports">Sign in</a> or <a href="/account/signup?next=/mods/{id}.html%23reports">create an account</a> to write a report.</p>
        <form class="report-form" method="post" action="/reports/{id}" data-report-form hidden>
          <input type="hidden" name="csrf" value="">
          <input type="hidden" name="back" value="/mods/{id}.html">
          <fieldset class="report-choice">
            <legend>Did it run for you in Horizon?</legend>
            <label><input type="radio" name="result" value="works" required> It works</label>
            <label><input type="radio" name="result" value="partly"> Partly</label>
            <label><input type="radio" name="result" value="broken"> It doesn't work</label>
          </fieldset>
          <p class="report-field"><label for="report-build">Kiln build</label><select id="report-build" name="channel"><option value="release">The latest release</option><option value="dev">The current development build</option></select></p>
          <p class="report-field"><label for="report-version">Mod version</label><input id="report-version" name="version" maxlength="64" autocomplete="off" spellcheck="false" value="{version}"></p>
          <p class="report-field"><label for="report-text">What you saw, optional</label><textarea id="report-text" name="text" maxlength="400" rows="4"></textarea></p>
          <p class="report-note">Shown at once with your name and the date. One report per build: another for the same build replaces it.</p>
          <p class="report-actions"><button type="submit" class="report-send">Send the report</button> <button type="button" class="report-delete" data-report-delete hidden>Delete my report for this build</button> <span class="report-status" data-report-status aria-live="polite"></span></p>
        </form>
      </section>
"""


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{canonical}">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f3eee6">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#15110d">
<link rel="icon" href="../favicon.ico" sizes="48x48">
<link rel="icon" href="../favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="../apple-touch-icon.png">
<link rel="manifest" href="../site.webmanifest">
<link rel="preload" href="../archivo.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="../archivo-expanded.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="../style.css">
<script>document.documentElement.classList.add("js");</script>
<script src="../site.js" defer></script>
<script src="../reports.js" defer></script>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>

<header class="site-header">
  <div class="doc header-row">
    <a class="brand" href="../index.html" aria-label="Kiln, home page">Kiln</a>
    <nav class="site-nav" aria-label="Sections">
      <a href="../index.html#how">How it works</a>
      <a href="../index.html#browser">Mod browser</a>
      <a href="../index.html#news">What's new</a>
      <a href="../index.html#roadmap">Roadmap</a>
      <a href="../index.html#faq">FAQ</a>
      <a href="../index.html#contact">Contact</a>
      <a href="../mods.html">Mods</a>
      <a href="https://github.com/Manager1215/horizon-kiln">GitHub</a>
    </nav>
    <a class="account-link" href="/account/signin" data-account-link>Sign in</a>
  </div>
  <div class="heat-line" aria-hidden="true"></div>
</header>

<main id="main" class="doc">
  <p class="crumb"><a href="../mods.html">All mods</a></p>
  <div class="sheet mod-page">
    <div class="sheet-head">
      <p class="mod-page-status"><span class="mark {mark}">{status_label}</span>{next_mark}</p>
{facts}    </div>
    <div class="sheet-body">
      <h1 class="page-title">{name}</h1>
{meaning}{sections}    </div>
  </div>
</main>

<footer class="site-footer">
  <div class="doc footer-grid">
    <div>
      <p class="footer-brand">Kiln</p>
      <p>Java mods in Horizon.</p>
      <p>License: <a href="https://github.com/Manager1215/horizon-kiln/blob/main/LICENSE">GPL-3.0</a> (0.1.0-alpha was MIT).</p>
      <p>The GPL keeps Kiln open, not people away: to use its work in your project, or just to talk, write to <a href="mailto:horizonkiln@gmail.com">horizonkiln@gmail.com</a> or Discord <strong>manager1215</strong>.</p>
    </div>
    <nav aria-label="Project links">
      <ul>
        <li><a href="../index.html">Home</a></li>
        <li><a href="../mods.html">Supported mods</a></li>
        <li><a href="https://github.com/Manager1215/horizon-kiln">Source on GitHub</a></li>
      </ul>
    </nav>
    <nav aria-label="Documents">
      <ul>
        <li><a href="https://github.com/Manager1215/horizon-kiln/releases">Releases</a></li>
        <li><a href="https://github.com/Manager1215/horizon-kiln/blob/main/INSTALL.md">Install guide (INSTALL.md)</a></li>
        <li><a href="https://github.com/horizon-rust/horizon">Horizon</a></li>
      </ul>
    </nav>
  </div>
  <div class="doc">
    <p class="disclaimer">Not affiliated with Horizon, Mojang or Microsoft. Minecraft is a trademark of Mojang/Microsoft. The timer is not an official speedrun.com timer.</p>
    <p class="disclaimer">The one cookie here keeps you signed in, and is set only when you sign in. <a href="../privacy.html">Your data</a>.</p>
  </div>
</footer>
</body>
</html>
"""


def next_mark(m):
    dev = m.get("development") or {}
    if not dev or dev.get("status") == m.get("status"):
        return ""
    return ' <span class="mark mark-next %s">next: %s</span>' % (MARK.get(dev.get("status"), "mark-unknown"), esc(dev.get("statusLabel") or dev.get("status") or ""))


def render(m):
    facts = "".join('        <div><dt>%s</dt><dd>%s</dd></div>\n' % (esc(term), value) for term, value in facts_of(m))
    if facts:
        facts = '      <dl class="mod-facts">\n' + facts + "      </dl>\n"
    meaning = '      <p class="mod-page-meaning">%s</p>\n' % esc(m["statusMeaning"]) if m.get("statusMeaning") else ""
    sections = "".join('      <section class="mod-section">\n        <h2>%s</h2>\n        %s\n      </section>\n' % (esc(title), body)
                       for title, body in sections_of(m)) + development_section(m) + REPORTS.replace("{id}", m["id"]).replace("{version}", esc(m.get("version") or ""))
    return PAGE.format(
        title=esc(m.get("name", m["id"]) + " — Kiln mods"),
        description=esc(description_of(m)),
        canonical=esc("%s/mods/%s.html" % (SITE, m["id"])),
        mark=MARK.get(m.get("status"), "mark-unknown"),
        status_label=esc(m.get("statusLabel") or m.get("status") or ""),
        next_mark=next_mark(m),
        facts=facts,
        name=esc(m.get("name", m["id"])),
        meaning=meaning,
        sections=sections,
        id=m["id"],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=".", help="the site's folder (mods.json and mods/ live here)")
    args = parser.parse_args()
    index = json.load(open(os.path.join(args.out, "mods.json"), encoding="utf-8"))
    mods_dir = os.path.join(args.out, "mods")
    written = 0
    urls = []
    wanted = set()
    for row in index.get("mods", []):
        mod_id = row.get("id", "")
        if not ID.match(mod_id):
            continue
        path = os.path.join(mods_dir, mod_id + ".json")
        if not os.path.isfile(path):
            continue
        m = json.load(open(path, encoding="utf-8"))
        m["id"] = mod_id
        page = render(m)
        target = os.path.join(mods_dir, mod_id + ".html")
        wanted.add(mod_id + ".html")
        old = open(target, encoding="utf-8").read() if os.path.isfile(target) else None
        if old != page:
            with open(target, "w", encoding="utf-8") as f:
                f.write(page)
            written += 1
        lastmod = ((m.get("checked") or {}).get("date") or (m.get("scanned") or {}).get("date") or index.get("updated") or "")
        urls.append((SITE + "/mods/" + mod_id + ".html", lastmod))
    # pages for mods no longer in the index go away
    removed = 0
    for name in os.listdir(mods_dir):
        if name.endswith(".html") and name not in wanted:
            os.unlink(os.path.join(mods_dir, name))
            removed += 1
    updated = index.get("updated", "")
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in [(SITE + "/", updated), (SITE + "/mods.html", updated)] + urls:
        lines.append("  <url><loc>%s</loc>%s</url>" % (esc(loc), "<lastmod>%s</lastmod>" % esc(lastmod) if lastmod else ""))
    lines.append("</urlset>")
    with open(os.path.join(args.out, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    robots = os.path.join(args.out, "robots.txt")
    if not os.path.isfile(robots):
        with open(robots, "w", encoding="utf-8") as f:
            f.write("User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % SITE)
    print("pages: %d written, %d unchanged, %d removed; sitemap: %d urls" % (written, len(urls) - written, removed, len(urls) + 2))


if __name__ == "__main__":
    main()
