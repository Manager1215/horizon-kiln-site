#!/usr/bin/env python3
"""Brings the site's data up to date from the Kiln repository, and refuses to break it.

    python3 .github/scripts/build-data.py --catalogue /tmp/catalogue --out .

The catalogue (mods.json, mods/<id>.json, stats.json; with --catalogue-dev the development layer is folded in) comes from the `catalogue` branch of
https://github.com/Manager1215/horizon-kiln, checked out into --catalogue by the workflow; the
changelog is read from CHANGELOG.md on main, with release links checked against the tags. A
prerelease (0.2.0-beta.1) leaves the list once its release (0.2.0) is in it, and
changelog-overrides.json in the site folder gives a release the summary and items the site shows
in place of the changelog's own, where those were written for testers rather than players.
--hand-checks lays the mods tried in the game over both layers as soon as they are on the Kiln
repository's main (its catalogue/hand-checks.py, D-083): a check on a released Kiln over the release
layer, a check on main over the development layer. Without --catalogue they go over the site's own
data; a release catalogue of the same Kiln that counts far fewer mods than the site (a scan that ran
out of time) is refused and the site's release layer kept.
Everything is validated before anything is written, and files are replaced atomically, so a
bad night leaves the site on its last good data. Exit code 0 means the data is good (changed
or not); anything else means nothing was written.
"""

import argparse
import json
import os
import re
import unicodedata
import shutil
import subprocess
import sys
import tempfile
import urllib.request

REPO = "Manager1215/horizon-kiln"
CHANGELOG_URL = "https://raw.githubusercontent.com/%s/main/CHANGELOG.md" % REPO
STATUSES = {"works", "may-work", "planned", "no-plans", "not-checked", "doesnt-work", "in-development"}
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_RELEASES = 10


class Bad(Exception):
    pass


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def clean_name(title):
    """A mod's name without the emoji and pictographs Modrinth titles carry: "Day Counter 🌞✅" is "Day Counter"."""
    out = []
    for ch in str(title or ""):
        code = ord(ch)
        if (unicodedata.category(ch) in ("So", "Cs", "Co", "Cn") or code == 0x200D or 0xFE00 <= code <= 0xFE0F
                or 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF or 0x1F3FB <= code <= 0x1F3FF):
            out.append(" ")
        else:
            out.append(ch)
    text = re.sub(r"\s+", " ", "".join(out)).strip()
    if "".join(out) != str(title or ""):  # a symbol went: a separator it hung on goes too ("Day Counter -" -> "Day Counter")
        text = re.sub(r"[\s\-–—:|·]+$", "", text).strip()
    return text or str(title or "")


# --- the catalogue -----------------------------------------------------------------------------

def check_catalogue(folder):
    """Reads and checks the scanner's output; returns (index, {id: detail}, stats or None)."""
    index_path = os.path.join(folder, "mods.json")
    if not os.path.isfile(index_path):
        raise Bad("no mods.json in the catalogue")
    if os.path.getsize(index_path) > MAX_INDEX_BYTES:
        raise Bad("mods.json is unreasonably large")
    index = read_json(index_path)
    mods = index.get("mods")
    if not isinstance(mods, list) or not mods:
        raise Bad("mods.json has no mods")
    if not isinstance(index.get("statuses"), dict):
        raise Bad("mods.json has no statuses")
    details = {}
    seen = set()
    for row in mods:
        mod_id = row.get("id")
        if not isinstance(mod_id, str) or not ID.match(mod_id):
            raise Bad("bad mod id: %r" % (mod_id,))
        if mod_id in seen:
            raise Bad("duplicate mod id: " + mod_id)
        seen.add(mod_id)
        if not isinstance(row.get("name"), str) or not row["name"].strip():
            raise Bad("mod without a name: " + mod_id)
        row["name"] = clean_name(row["name"])
        if row.get("status") not in STATUSES:
            raise Bad("unknown status %r for %s" % (row.get("status"), mod_id))
        path = os.path.join(folder, "mods", mod_id + ".json")
        if not os.path.isfile(path):
            raise Bad("no file for " + mod_id)
        detail = read_json(path)
        if detail.get("id") != mod_id or detail.get("status") not in STATUSES:
            raise Bad("mods/%s.json does not match the index" % mod_id)
        if isinstance(detail.get("name"), str):
            detail["name"] = clean_name(detail["name"])
        details[mod_id] = detail
    stats = None
    stats_path = os.path.join(folder, "stats.json")
    if os.path.isfile(stats_path):
        stats = read_json(stats_path)
        if not isinstance(stats, dict):
            raise Bad("stats.json is not an object")
    return index, details, stats


DEV_KEYS = ("status", "statusLabel", "statusMeaning", "evidence", "needs", "roadmap", "run", "works", "note",
            "checked", "url", "version", "lacks", "scanned")


def merge_development(index, details, stats, dev_index, dev_details, dev_stats):
    """Folds the development layer into the release one: every row keeps its release status and gains `next`
    when main says something else; every detail gains `development`; the index names the build."""
    build = dev_index.get("kiln", "")
    by_id = {row["id"]: row for row in index["mods"]}
    for dev_row in dev_index["mods"]:
        mod_id = dev_row["id"]
        row = by_id.get(mod_id)
        if row is None:
            # a mod the release scan has not seen yet: it enters as not checked for the release
            row = {"id": mod_id, "name": dev_row["name"], "version": dev_row.get("version", ""), "status": "not-checked"}
            index["mods"].append(row)
            by_id[mod_id] = row
            detail = dict(dev_details[mod_id])
            detail.update({"status": "not-checked", "statusLabel": index["statuses"].get("not-checked", "Not checked"),
                           "statusMeaning": "Not scanned against the release yet.", "evidence": "", "needs": [], "roadmap": ""})
            details[mod_id] = detail
        dev_detail = dev_details[mod_id]
        if dev_row.get("status") != row.get("status"):
            row["next"] = {k: v for k, v in dev_row.items() if k in ("status", "run", "lacks", "stage", "works", "note", "data", "candidate")}
        elif "next" in row:
            del row["next"]
        details[mod_id]["development"] = dict({k: dev_detail[k] for k in DEV_KEYS if k in dev_detail}, build=build)
    index["mods"].sort(key=lambda r: r["name"].lower())
    index["development"] = {"build": build, "updated": dev_index.get("updated", ""), "counts": dev_stats.get("counts", {}) if dev_stats else {}}
    if stats is not None and dev_stats is not None:
        stats["development"] = {k: dev_stats[k] for k in ("updated", "kiln", "counts", "top_missing") if k in dev_stats}


# --- the hand checks ---------------------------------------------------------------------------

HAND_KEYS = ("name", "version", "status", "statusLabel", "statusMeaning", "works", "note", "url", "checked")
LAYERS = ("release", "dev", "both")
SHRINK = 0.9  # a release catalogue of the same Kiln with fewer than this share of the counted mods is refused


def check_hand_checks(path):
    """Reads and checks hand-checks.json from the Kiln repository (catalogue/hand-checks.py, D-083)."""
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("mods"), list):
        raise Bad("hand-checks.json is not schema 1 with a list of mods")
    seen = set()
    for h in data["mods"]:
        if not isinstance(h, dict) or not isinstance(h.get("id"), str) or not ID.match(h["id"]):
            raise Bad("hand check with a bad id: %r" % (h.get("id") if isinstance(h, dict) else h,))
        if h["id"] in seen:
            raise Bad("hand check twice: " + h["id"])
        seen.add(h["id"])
        if h.get("layer") not in LAYERS:
            raise Bad("hand check %s: unknown layer %r" % (h["id"], h.get("layer")))
        if h.get("status") not in STATUSES:
            raise Bad("hand check %s: unknown status %r" % (h["id"], h.get("status")))
        if not isinstance(h.get("name"), str) or not h["name"].strip():
            raise Bad("hand check %s: no name" % h["id"])
        if not isinstance(h.get("checked", {}), dict):
            raise Bad("hand check %s: checked is not an object" % h["id"])
    return data


def short_row(mod_id, fields):
    row = {"id": mod_id, "name": fields["name"], "version": fields.get("version", ""), "status": fields["status"]}
    for key in ("works", "note"):
        if fields.get(key):
            row[key] = fields[key]
    return row


def apply_hand_checks(index, details, hand):
    """Lays the hand checks over the layers, newer than either catalogue: a check on a released Kiln over the
    release layer (and the development one), a check on main over the development layer alone. Returns the ids."""
    by_id = {row["id"]: row for row in index["mods"]}
    rows = {r["id"]: i for i, r in enumerate(index["mods"])}
    applied = []
    for h in hand["mods"]:
        mod_id = h["id"] if h["id"] in by_id else h.get("scanId") if h.get("scanId") in by_id else h["id"]
        fields = {k: h[k] for k in HAND_KEYS if k in h}
        fields["name"] = clean_name(fields["name"])
        fields.setdefault("statusLabel", index["statuses"].get(h["status"], h["status"]))
        row = by_id.get(mod_id)
        if row is None:
            status = "not-checked" if h["layer"] == "dev" else h["status"]
            row = {"id": mod_id, "name": fields["name"], "version": fields.get("version", ""), "status": status}
            index["mods"].append(row)
            rows[mod_id] = len(index["mods"]) - 1
            by_id[mod_id] = row
            details[mod_id] = {"id": mod_id, "name": fields["name"], "version": fields.get("version", ""),
                               "status": status, "statusLabel": index["statuses"].get(status, status),
                               "statusMeaning": "Not scanned against the release yet." if status == "not-checked" else "",
                               "needs": [], "reports": {"works": 0, "partly": 0, "broken": 0, "latest": []}}
        detail = details[mod_id]
        development = dict({k: v for k, v in fields.items()}, build=h.get("build") or (index.get("development") or {}).get("build", ""))
        if h["layer"] in ("release", "both"):
            detail.update(fields)
            detail.update({"needs": [], "roadmap": "", "evidence": ""})
            for key in ("works", "note", "url", "checked"):
                if key not in fields:
                    detail.pop(key, None)
            new_row = short_row(mod_id, fields)
            index["mods"][rows[mod_id]] = new_row
            by_id[mod_id] = new_row
            if "development" in detail or "development" in index:
                detail["development"] = development
        else:
            row = by_id[mod_id]
            if h["status"] != row["status"]:
                row["next"] = {k: fields[k] for k in ("status", "works", "note") if k in fields}
            else:
                row.pop("next", None)
            detail["development"] = development
            if "development" not in index:
                index["development"] = {"build": development["build"], "updated": hand.get("updated", ""), "counts": {}}
        applied.append(mod_id)
    index["mods"].sort(key=lambda r: r["name"].lower())
    index["handChecks"] = {"updated": hand.get("updated", ""), "commit": hand.get("commit", ""), "mods": len(applied)}
    return applied


def recount(index, stats):
    """The counts after the hand checks: the release layer's, and the development layer's where there is one."""
    counts, dev_counts = {}, {}
    for row in index["mods"]:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        st = (row.get("next") or {}).get("status") or row["status"]
        dev_counts[st] = dev_counts.get(st, 0) + 1
    if "development" in index:
        index["development"]["counts"] = dev_counts
    if stats is not None:
        stats["counts"] = counts
        if "development" in stats:
            stats["development"]["counts"] = dev_counts


def counted(index):
    return sum(1 for row in index["mods"] if row.get("status") in ("works", "may-work"))


def not_shrunk(index, current_folder):
    """None when the new release catalogue is fit to replace the site's; the reason otherwise. A first scan in the
    public repository that ran out of time leaves mods unrun: the same Kiln with far fewer mods counted is that."""
    path = os.path.join(current_folder, "mods.json")
    if not os.path.isfile(path):
        return None
    try:
        current = read_json(path)
    except (OSError, ValueError):
        return None
    if current.get("kiln") != index.get("kiln"):
        return None
    before, after = counted(current), counted(index)
    if before and after < SHRINK * before:
        return "the release catalogue counts %d mods that work or should work on Kiln %s, the site %d" % (
            after, index.get("kiln", "?"), before)
    return None


# --- the changelog -----------------------------------------------------------------------------

HEADING = re.compile(r"^##\s+(\S+)\s*[—–-]\s*(\d{4}-\d{2}-\d{2})\s*$")
MARKDOWN = [
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
]


def plain(text):
    for pattern, replacement in MARKDOWN:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text).strip()


def first_sentence(text):
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return match.group(1) if match else text


def parse_changelog(markdown, tags):
    """Releases from CHANGELOG.md: `## <version> — <date>`, a paragraph, then bullets."""
    releases = []
    current = None
    paragraph = []
    bullet = None

    def flush_bullet():
        nonlocal bullet
        if current is not None and bullet:
            current["items"].append(first_sentence(plain(bullet)))
        bullet = None

    def flush_paragraph():
        nonlocal paragraph
        if current is not None and paragraph and not current["summary"]:
            current["summary"] = plain(" ".join(paragraph))
        paragraph = []

    for line in markdown.splitlines():
        heading = HEADING.match(line)
        if heading:
            flush_bullet()
            flush_paragraph()
            version, date = heading.group(1), heading.group(2)
            tag = "v" + version
            current = {
                "version": version,
                "date": date,
                "url": "https://github.com/%s/releases/tag/%s" % (REPO, tag) if tag in tags
                else "https://github.com/%s/blob/main/CHANGELOG.md" % REPO,
                "summary": "",
                "items": [],
            }
            releases.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            flush_bullet()
            flush_paragraph()
            bullet = stripped[2:]
        elif stripped and bullet is not None and line.startswith((" ", "\t")):
            bullet += " " + stripped
        elif stripped:
            flush_bullet()
            paragraph.append(stripped)
        else:
            flush_bullet()
            flush_paragraph()
    flush_bullet()
    flush_paragraph()
    if not releases:
        raise Bad("CHANGELOG.md has no release headings")
    released = {r["version"] for r in releases}
    releases = [r for r in releases if r["version"].split("-")[0] == r["version"]
                or r["version"].split("-")[0] not in released]
    return releases[:MAX_RELEASES]


def apply_overrides(releases, path):
    """The site's own words for some releases: {"0.2.0": {"summary": "...", "items": ["..."]}}."""
    if not os.path.exists(path):
        return releases
    overrides = read_json(path)
    if not isinstance(overrides, dict):
        raise Bad("%s is not an object" % path)
    for version, override in overrides.items():
        if not isinstance(override, dict) or not isinstance(override.get("summary", ""), str) \
                or not isinstance(override.get("items", []), list) \
                or not all(isinstance(i, str) for i in override.get("items", [])):
            raise Bad("%s: %s needs a summary string and a list of item strings" % (path, version))
    for release in releases:
        override = overrides.get(release["version"])
        if override:
            release["summary"] = override.get("summary") or release["summary"]
            release["items"] = override.get("items") or release["items"]
    return releases


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "horizon-kiln-site build-data"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def tags_of(repo):
    try:
        output = subprocess.run(["git", "ls-remote", "--tags", "https://github.com/%s.git" % repo],
                                check=True, capture_output=True, text=True, timeout=120).stdout
    except (subprocess.SubprocessError, OSError) as error:
        print("could not list tags:", error, file=sys.stderr)
        return set()
    tags = set()
    for line in output.splitlines():
        ref = line.split("\t")[-1]
        if ref.startswith("refs/tags/"):
            tags.add(ref[len("refs/tags/"):].removesuffix("^{}"))
    return tags


# --- main --------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalogue", help="folder holding the catalogue branch (mods.json, mods/, stats.json)")
    parser.add_argument("--catalogue-dev", help="folder holding the development layer: the same files, scanned and run against main")
    parser.add_argument("--hand-checks", help="hand-checks.json from the Kiln repository: the mods tried in the game, over both layers")
    parser.add_argument("--changelog", help="a local CHANGELOG.md instead of fetching it")
    parser.add_argument("--out", default=".", help="the site folder")
    parser.add_argument("--skip-changelog", action="store_true")
    args = parser.parse_args()
    changed = []

    try:
        hand = check_hand_checks(args.hand_checks) if args.hand_checks else None
        catalogue = args.catalogue
        if catalogue:
            index, details, stats = check_catalogue(catalogue)
            shrunk = None if os.path.samefile(catalogue, args.out) else not_shrunk(index, args.out)
            if shrunk:
                print("keeping the site's release layer:", shrunk, file=sys.stderr)
                catalogue = args.out
        elif hand or args.catalogue_dev:
            catalogue = args.out  # no release catalogue yet: the layers newer than it go over the site's own data
            print("no catalogue folder given; the newer layers go over the site's own data")
        if catalogue:
            if catalogue != args.catalogue:
                index, details, stats = check_catalogue(catalogue)
            if args.catalogue_dev:
                merge_development(index, details, stats, *check_catalogue(args.catalogue_dev))
            if hand:
                applied = apply_hand_checks(index, details, hand)
                recount(index, stats)
                changed.append("%d hand checks" % len(applied))
            staged = tempfile.mkdtemp(prefix="kiln-mods-", dir=args.out)
            os.chmod(staged, 0o755)  # mkdtemp gives 700, and rsync -p would carry that to the server: nothing readable
            for mod_id, detail in details.items():
                write_json(os.path.join(staged, mod_id + ".json"), detail)
            target = os.path.join(args.out, "mods")
            old = target + ".old"
            if os.path.isdir(old):
                shutil.rmtree(old)
            if os.path.isdir(target):
                os.rename(target, old)
            os.rename(staged, target)
            if os.path.isdir(old):
                shutil.rmtree(old)
            write_json(os.path.join(args.out, "mods.json"), index)
            if stats is not None:
                write_json(os.path.join(args.out, "stats.json"), stats)
            changed.append("%d mods" % len(index["mods"]))
        else:
            print("no catalogue folder given; the mods stay as they are")

        if not args.skip_changelog:
            markdown = open(args.changelog, encoding="utf-8").read() if args.changelog else fetch(CHANGELOG_URL)
            releases = apply_overrides(parse_changelog(markdown, tags_of(REPO)),
                                       os.path.join(args.out, "changelog-overrides.json"))
            write_json(os.path.join(args.out, "changelog.json"), {
                "schema": 1,
                "source": "https://github.com/%s/blob/main/CHANGELOG.md" % REPO,
                "releases": releases,
            })
            changed.append("%d releases" % len(releases))
    except Bad as error:
        print("refusing to update the site's data:", error, file=sys.stderr)
        return 1
    except (OSError, ValueError, urllib.error.URLError) as error:
        print("could not build the site's data:", error, file=sys.stderr)
        return 1
    print("site data built:", ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
