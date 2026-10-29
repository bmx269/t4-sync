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
# Every path this tool asks for, with the method and the reason. Written out
# by hand: a regex over the source would miss the ones built by string
# formatting, which is most of them, and quietly report a dependency as
# absent. `--{lang}` is spelled `{language}` to match the specification.
USED = [
    ("GET", "/pageLayout", "page layout metadata"),
    ("GET", "/pageLayout/{id}", "page layout source"),
    ("GET", "/navigation", "navigation objects"),
    ("GET", "/navigation/{id}", "one navigation object"),
    ("GET", "/contenttype", "content types and their elements"),
    ("GET", "/channel", "channels"),
    ("GET", "/channel/{id}", "one channel"),
    ("GET", "/list/{id}", "lists; /list alone answers 500, so list/1 is used"),
    ("GET", "/sitestructure", "the whole section tree in one response"),
    ("GET", "/layout/contenttype/{id}/{language}",
     "a content type's layouts"),
    ("GET", "/layout/{id}/{language}", "one content layout"),
    ("GET", "/hierarchy/{id}/{language}", "one section"),
    ("GET", "/hierarchy/{id}/{language}/subsections", "its children"),
    ("GET", "/hierarchy/{id}/{language}/contents", "content placed in it"),
    ("GET", "/media/{id}/{language}", "media, including text media source"),
    ("GET", "/media/{id}/{language}/{version}/{element}",
     "binary media, via the signed URL it answers with"),
    ("PUT", "/pageLayout/{id}", "push a page layout"),
    ("PUT", "/layout/{id}/{language}", "push a content layout"),
    ("PUT", "/media/{id}/{language}", "push text media"),
]

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

# Two sources, because neither is complete on its own.
#
# The OpenAPI document under /new/ is the better evidence: exact paths and
# methods rather than headings scraped out of HTML. But it is a curated
# *collection* -- fifteen groups -- and leaves out several paths this tool
# depends on.
#
# The older page is the full surface, a hundred-odd groups, but it is
# generated HTML with no methods and it names a path by flattening it
# (/hierarchy{id}{language}contents), so it can only be matched loosely.
SPEC = "https://webapi.terminalfour.com/new/openapi.json"
REFERENCE = "https://webapi.terminalfour.com/"

GROUP_RE = re.compile(
    r'<h1\s+id="terminalfour-rest-api-[^"]*">\s*([^<]+?)\s*</h1>', re.I)
VERSION_RE = re.compile(r"TERMINALFOUR REST API\s+v?([\w.-]+)", re.I)


def read(url, ranged=False):
    """Fetch a URL, working around a connection that is cut short.

    Both documents are large and the connection routinely closes before the
    declared length. For the HTML page a partial body is still useful. For
    the JSON it is not -- a truncated document will not parse -- so the
    remainder is asked for with a Range header until it does.
    """
    def once(start=None):
        headers = {"User-Agent": UA}
        if start:
            headers["Range"] = "bytes=%d-" % start
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read(), False
        except http.client.IncompleteRead as exc:
            return exc.partial, True
        except urllib.error.HTTPError as exc:
            print("  ! %s %s" % (exc.code, url))
        except Exception as exc:                      # noqa: BLE001
            print("  ! %s %s" % (type(exc).__name__, url))
        return b"", False

    body, short = once()
    if not ranged:
        return body.decode("utf-8", errors="replace"), short
    for _ in range(25):
        try:
            return json.loads(body), False
        except ValueError:
            more, _short = once(len(body))
            if not more:
                return None, True
            body += more
    return None, True


def spec_paths(spec):
    """{path: [METHOD]} from an OpenAPI document."""
    found = {}
    for path, operations in (spec.get("paths") or {}).items():
        found[path] = sorted(
            method.upper() for method in operations
            if method.lower() in ("get", "post", "put", "patch", "delete"))
    return found


def groups(page):
    """Every top-level resource group named in the older reference."""
    return {html.unescape(name).strip()
            for name in GROUP_RE.findall(page)
            if name.strip() and len(name.strip()) > 1}


def version(page):
    found = VERSION_RE.search(html.unescape(page))
    return found.group(1) if found else None


def flat(path):
    """A path as the older page spells it: segments run together.

    That page lists /hierarchy/{id}/{language}/contents as
    `hierarchy{id}{language}contents`, so a path can only be looked for
    there with its punctuation removed.
    """
    return re.sub(r"[^a-z0-9]", "", path.lower())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true",
                        help="list every group, not only the notable ones")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    args = parser.parse_args()

    spec, spec_short = read(SPEC, ranged=True)
    page, page_short = read(REFERENCE)
    if spec is None and not page:
        print("Could not read either document; nothing checked.")
        return 1

    paths = spec_paths(spec) if spec else {}
    documented = groups(page) if page else set()
    listed = {flat(name) for name in documented}
    # The older page names a group, not its paths, so a path counts as
    # present there if the page spells out that exact path or its group.
    flat_page = flat(page) if page else ""

    verdicts = []
    for method, path, why in USED:
        in_spec = path in paths
        allowed = method in paths.get(path, [])
        if in_spec and allowed:
            state, where = "ok", "specification"
        elif in_spec:
            state, where = "METHOD", "specification lists %s only"\
                % ",".join(paths[path])
        elif flat(path) in flat_page:
            state, where = "ok", "older reference"
        elif flat(path.strip("/").split("/")[0]) in listed:
            state, where = "group", "older reference lists the group only"
        else:
            state, where = "MISSING", "neither document"
        verdicts.append({"method": method, "path": path, "why": why,
                         "state": state, "where": where})

    missing = [v for v in verdicts if v["state"] == "MISSING"]
    wrong = [v for v in verdicts if v["state"] == "METHOD"]
    notable = sorted(name for name in NOTABLE if name in documented
                     and not any(v["path"].startswith("/" + name)
                                 for v in verdicts))

    if args.json:
        json.dump({"spec_paths": len(paths), "groups": sorted(documented),
                   "used": verdicts, "notable": notable},
                  sys.stdout, indent=2)
        print()
        return 1 if missing or wrong else 0

    said = version(page) if page else None
    print("Specification  %s  (%d paths)"
          % (SPEC, len(paths)) if paths else
          "Specification  could not be read")
    print("Reference      %s  (%s, %d groups)%s"
          % (REFERENCE, said or "version not stated", len(documented),
             "  [read short]" if page_short else ""))
    print()

    print("WHAT THIS TOOL CALLS")
    for v in verdicts:
        print("  %-4s %-44s %-8s %s"
              % (v["method"], v["path"], v["state"], v["where"]))

    if wrong:
        print("\n%d path(s) are documented but not for the method used."
              % len(wrong))
    if missing:
        print("\n%d path(s) appear in neither document." % len(missing))
        print("That may be a rename, a removal, or these scripts reading a\n"
              "page wrongly. `t4 doctor` against a real instance is the\n"
              "stronger signal; check by hand before believing this.")
    if not missing and not wrong:
        print("\nEvery path this tool calls is documented.")

    print("\nNOT USED, AND WORTH KNOWING ABOUT")
    for name in notable:
        print("  %-18s %s" % (name, NOTABLE[name]))

    rest = sorted(name for name in documented
                  if name not in NOTABLE
                  and not any(v["path"].startswith("/" + name)
                              for v in verdicts))
    if args.all:
        print("\nEVERY OTHER GROUP (%d)" % len(rest))
        for index in range(0, len(rest), 4):
            print("  " + "".join("%-24s" % n for n in rest[index:index + 4]))
    else:
        print("\n%d further group(s) unused — mostly administration."
              " Use --all to list them." % len(rest))

    print("\nNeither document says what a deployment routes, and they do not\n"
          "agree with each other: the specification is a curated collection\n"
          "and the older reference is the full surface without methods.")
    if said and not said.startswith("8.4"):
        print("Neither is labelled 8.4.x, which is what this tool is verified\n"
              "against, so an absence here proves nothing. Run `t4 doctor`.")
    return 1 if missing or wrong else 0


if __name__ == "__main__":
    sys.exit(main())
