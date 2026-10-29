#!/usr/bin/env python3
"""Check what this tool uses against Terminal Four's published API reference.

    python3 docs/check_api.py
    python3 docs/check_api.py --all        # every group, not just the notable
    python3 docs/check_api.py --json       # for a script to read

The reference at

    webapi.terminalfour.com

lists around a hundred top-level resource groups. This tool deliberately
touches a dozen: it is a local development tool, and most of that surface is
administration a thing running on someone's laptop has no business calling.
So the interesting output is not "what is missing" -- almost everything is --
but whether the reference still lists the groups we *do* depend on, and
whether anything has appeared that a development tool would want.

Deliberately not a test: it needs the network, and the tests must not. Nor
does it prove anything works. The reference says what T4 publishes; a given
deployment routes some subset of it, with its own casing, which is what
`t4 doctor` finds out by asking the instance. Read this for what changed in
the documentation, and run doctor for what a server actually answers.
"""
import argparse
import html
import http.client
import json
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve()
                       .parents[1] / "src"))

from t4sync.endpoints import ENDPOINTS  # noqa: E402

BASE = "https://webapi.terminalfour.com/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

# Every path the tool asks for, with the module that asks. Kept here rather
# than scraped out of the source: a regex over the code would miss the ones
# built by string formatting, which is most of them, and quietly report a
# dependency as absent.
USED = {
    "pageLayout": "endpoints.py -- page layout source",
    "navigation": "endpoints.py -- navigation objects",
    "contenttype": "endpoints.py -- content types",
    "channel": "endpoints.py -- channels",
    "list": "endpoints.py -- lists (as list/1; /list alone answers 500)",
    "sitestructure": "endpoints.py -- the section tree in one response",
    "layout": "contentlayout.py -- layout/{id}/{lang}, "
              "layout/contenttype/{id}/{lang}",
    "hierarchy": "sections.py -- hierarchy/{id}/{lang}, /subsections, "
                 "/contents",
    "media": "pull.py -- media/{id}/{lang}, and binary via a signed URL",
}

# Groups a local development tool might plausibly grow into, as opposed to
# the administration surface. Listing them is not a proposal to use them;
# it is so a reader can see the choice was made rather than overlooked.
NOTABLE = {
    "content": "content items themselves; the tool reaches them only "
               "through hierarchy/{id}/{lang}/contents",
    "section": "sections; the tool uses hierarchy/ instead",
    "version": "version history of a record -- would let a diff go further "
               "back than the last pull",
    "lock": "record locking -- a push could take a lock rather than hoping "
            "nobody is editing",
    "preview": "server-side preview, which is the one thing that renders "
               "T4 tags properly",
    "directedit": "Direct Edit",
    "search": "search",
    "cmssearch": "search across the CMS",
    "workflow": "approval workflow -- a push bypasses it today",
    "task": "tasks",
    "publishlogs": "what happened on the last publish",
    "publishProcessor": "publish processors",
    "dataobject": "data objects",
    "formbuilder": "forms",
    "user": "users",
    "group": "groups",
}

# The reference is one generated page: every resource group is an <h1>
# anchored under the document's own id, and every operation inside it an
# <h2>. The title carries the version the page was generated from.
GROUP_RE = re.compile(
    r'<h1\s+id="terminalfour-rest-api-[^"]*">\s*([^<]+?)\s*</h1>', re.I)
VERSION_RE = re.compile(r"TERMINALFOUR REST API\s+v?([\w.-]+)", re.I)


def fetch(url):
    """The reference, or as much of it as arrives.

    It is four megabytes of generated HTML and the connection is routinely
    cut short of the declared length. A truncated read still names most of
    the groups, so the partial body is used and the shortfall reported,
    rather than throwing away a mostly-complete answer.
    """
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read().decode("utf-8", errors="replace"), False
    except http.client.IncompleteRead as exc:
        return exc.partial.decode("utf-8", errors="replace"), True
    except urllib.error.HTTPError as exc:
        print("  ! %s %s" % (exc.code, url))
    except Exception as exc:                          # noqa: BLE001
        print("  ! %s %s" % (type(exc).__name__, url))
    return "", False


def groups(page):
    """Every top-level resource group named in the reference."""
    return {html.unescape(name).strip()
            for name in GROUP_RE.findall(page)
            if name.strip() and len(name.strip()) > 1}


def version(page):
    found = VERSION_RE.search(html.unescape(page))
    return found.group(1) if found else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true",
                        help="list every group, not only the notable ones")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    args = parser.parse_args()

    # USED is written out by hand, so it can drift from the table the tool
    # actually pulls with. Catch that here rather than reporting on a list
    # that no longer describes the code.
    adrift = sorted(set(ENDPOINTS) - set(USED))
    if adrift:
        print("USED is out of date: endpoints.py pulls %s, not listed here."
              % ", ".join(adrift))
        return 1

    page, truncated = fetch(BASE)
    if not page:
        print("Could not read the reference; nothing checked.")
        return 1
    documented = groups(page)
    if len(documented) < 20:
        print("Only %d group(s) found -- the reference's markup has probably\n"
              "changed and the patterns in this script need revisiting."
              % len(documented))
        return 1

    missing = sorted(name for name in USED if name not in documented)
    notable = sorted(name for name in NOTABLE
                     if name in documented and name not in USED)
    unused = sorted(documented - set(USED))

    if args.json:
        json.dump({"documented": sorted(documented), "used": sorted(USED),
                   "used_but_undocumented": missing, "notable": notable,
                   "unused": unused}, sys.stdout, indent=2)
        print()
        return 1 if missing else 0

    said = version(page)
    print("Reference  %s%s" % (said or "version not stated",
                               "  (read short -- some groups may be missing)"
                               if truncated else ""))
    print("Lists %d group(s). This tool uses %d.\n"
          % (len(documented), len(USED)))

    print("USED")
    for name in sorted(USED):
        state = "ok" if name in documented else "NOT IN THE REFERENCE"
        print("  %-16s %-21s %s" % (name, state, USED[name]))

    if missing:
        print("\n%d group(s) this tool depends on are no longer documented."
              % len(missing))
        print("That may be a rename, a removal, or this script reading the\n"
              "page wrongly. Check by hand before believing it; `t4 doctor`\n"
              "against a real instance is the stronger signal.")

    print("\nNOT USED, AND WORTH KNOWING ABOUT")
    for name in notable:
        print("  %-18s %s" % (name, NOTABLE[name]))

    if args.all:
        print("\nEVERY OTHER GROUP (%d)" % len(unused))
        for index in range(0, len(unused), 4):
            print("  " + "".join("%-24s" % n for n in unused[index:index + 4]))
    else:
        print("\n%d further group(s) unused -- mostly administration."
              " Use --all to list them." % (len(unused) - len(notable)))

    print("\nThis says what the documentation lists, not what a deployment\n"
          "routes. Run `t4 doctor` for that.")
    if said and not said.startswith("8.4"):
        print("\nThe published reference is %s. This tool is verified against\n"
              "8.4.x, so a deployment may route groups this page does not\n"
              "list, and the absence of one here proves nothing." % said)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
