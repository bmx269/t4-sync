#!/usr/bin/env python3
"""Check the tag tables against Terminal Four's published documentation.

    python3 docs/check_reference.py

The tables in `t4sync/tags.py` were transcribed by hand from

    docs.terminalfour.com/documentation/page-management/generate-t4-tags/

and a transcription is only as good as the day it was made. This walks that
page and every sub-page, pulls out every `type=`, `output=` and attribute it
can find in the examples and tables, and reports what the documentation has
that we do not, and what we claim that it never mentions.

Deliberately not a test: it needs the network, and the tests must not. Run it
when the tables are being changed, or when T4 publishes a new version, and
fold the findings in by hand -- the output is evidence to read, not a patch
to apply blindly. A term can be missing from here simply because the page
describes it in prose.
"""
import argparse
import collections
import html
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from t4sync import tags  # noqa: E402

BASE = "https://docs.terminalfour.com/documentation/page-management/generate-t4-tags/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

TYPE_RE = re.compile(r'type\s*=\s*&quot;([\w.-]+)&quot;|type\s*=\s*"([\w.-]+)"')
OUTPUT_RE = re.compile(r'output\s*=\s*&quot;([\w.-]+)&quot;|output\s*=\s*"([\w.-]+)"')
# An attribute inside a t4 tag example, however the page happens to escape it.
ATTR_RE = re.compile(r'([\w-]+)\s*=\s*(?:&quot;|")')


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        print("  ! %s %s" % (exc.code, url))
        return ""
    except Exception as exc:                          # noqa: BLE001
        print("  ! %s %s" % (type(exc).__name__, url))
        return ""


def sub_pages(index_html):
    found = []
    for href in re.findall(r'href="([^"]*generate-t4-tags/[^"#]+)"', index_html):
        if href.startswith("/"):
            href = "https://docs.terminalfour.com" + href
        if href.startswith(BASE) and href.rstrip("/") != BASE.rstrip("/"):
            found.append(href)
    return sorted(set(found))


def terms(page):
    """Everything in one page that looks like tag vocabulary.

    Only inside a t4 tag. Reading the whole page turned every HTML form
    example into a tag type -- "text", "submit" and "button" were reported
    as missing from the tables, which they rightly are.
    """
    found = {"type": set(), "output": set(), "attr": set()}
    for snippet in re.findall(r"(?:&lt;|<)t4\b(.{0,600}?)(?:/?(?:&gt;|>))",
                              page, re.S):
        for match in TYPE_RE.finditer(snippet):
            found["type"].add(match.group(1) or match.group(2))
        for match in OUTPUT_RE.finditer(snippet):
            found["output"].add(match.group(1) or match.group(2))
        for attr in ATTR_RE.findall(snippet):
            found["attr"].add(attr.lower())
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true",
                        help="only print the differences")
    args = parser.parse_args()

    index = fetch(BASE)
    if not index:
        print("Could not read the index; nothing checked.")
        return 1
    pages = [BASE] + sub_pages(index)
    print("Reading %d page(s)\n" % len(pages))

    seen = {"type": collections.Counter(), "output": collections.Counter(),
            "attr": collections.Counter()}
    for url in pages:
        page = index if url == BASE else fetch(url)
        if not page:
            continue
        found = terms(html.unescape(page) if "&lt;t4" not in page else page)
        for key, values in found.items():
            for value in values:
                seen[key][value] += 1
        if not args.quiet:
            print("  %-58s %d types, %d outputs, %d attrs"
                  % (url.rstrip("/").rsplit("/", 1)[-1] or "index",
                     len(found["type"]), len(found["output"]),
                     len(found["attr"])))

    ours = {
        "type": set(tags.TAG_TYPES),
        "output": set(tags.OUTPUTS),
        "attr": {attr for spec in tags.TAG_TYPES.values()
                 for attr in spec["attrs"]} | {"type", "modifiers"},
    }
    # A language variable carries one attribute per language, so those are
    # accepted by pattern rather than listed. Without this they look like
    # gaps on every run.
    for value in list(seen["attr"]):
        if tags.LANGUAGE_ATTR.match(value):
            ours["attr"].add(value)

    problems = 0
    for key in ("type", "output", "attr"):
        documented = set(seen[key])
        missing = sorted(documented - ours[key])
        extra = sorted(ours[key] - documented)
        print("\n%s" % key.upper())
        print("  in the docs: %d   in tags.py: %d" % (len(documented), len(ours[key])))
        if missing:
            problems += len(missing)
            print("  documented but not offered:")
            for value in missing:
                print("     %-26s seen on %d page(s)" % (value, seen[key][value]))
        if extra:
            print("  offered but not found in the examples (may be prose only):")
            print("     %s" % ", ".join(extra))
    print("\n%d term(s) documented and not offered." % problems)
    return 0


if __name__ == "__main__":
    sys.exit(main())
