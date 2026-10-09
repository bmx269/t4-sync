"""t4 compare -- check the local server renders what the live site serves.

Fetches a page from the published site and from the local server, removes
everything this tool injected, and diffs the two. It answers "is what I am
looking at locally actually the real page", which matters because the local
copy is assembled from a mirror plus local edits plus captured tag output --
several places for a difference to creep in unnoticed.

Differences are expected and explained rather than treated as failures:
local layout edits that have not been published yet will show up here, and so
will content that changed in T4 since the mirror was taken.
"""
import difflib
import sys
import urllib.error
import urllib.request

from ..errors import T4Error
from ..inspector import strip_injected


def get(url, timeout=30):
    request = urllib.request.Request(url)
    request.add_header("User-Agent", "t4-sync/compare")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def run(args, project):
    env_name = args.env or project.default_env()
    env = project.env(env_name)
    published = (args.live or env.get("published_url") or "").rstrip("/")
    if not published:
        raise T4Error("No published URL for %r. Pass --live or set "
                      "\"published_url\" in .t4/config.json" % env_name)

    path = "/" + (args.page or "").strip("/")
    if path != "/" and not path.endswith(("/", ".html", ".htm")):
        path += "/"
    local_base = args.local.rstrip("/")

    try:
        live = get(published + path, args.timeout)
    except urllib.error.HTTPError as exc:
        raise T4Error("live site returned HTTP %s for %s" % (exc.code, path))
    except Exception as exc:
        raise T4Error("could not reach %s%s (%s)\nA T4 site is usually behind "
                      "a VPN." % (published, path, type(exc).__name__))

    try:
        local_raw = get(local_base + path, args.timeout)
    except Exception as exc:
        raise T4Error("could not reach %s%s (%s)\nIs `t4 serve` running?"
                      % (local_base, path, type(exc).__name__))

    local = strip_injected(local_raw)

    print("page    %s" % path)
    print("live    %s%s  (%d bytes)" % (published, path, len(live)))
    print("local   %s%s  (%d raw, %d after removing annotations)"
          % (local_base, path, len(local_raw), len(local)))

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            fh.write(local)
        print("wrote   %s" % args.out)

    if live == local:
        print("\nIdentical. The local server renders exactly what the live "
              "site serves.")
        return 0

    diff = list(difflib.unified_diff(
        live.splitlines(), local.splitlines(),
        fromfile="live", tofile="local (annotations removed)",
        n=args.context, lineterm=""))
    changed = [l for l in diff
               if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    print("\n%d differing line(s), %+d bytes\n" % (len(changed), len(local) - len(live)))

    limit = args.limit
    for line in diff[:limit]:
        print(line[:200])
    if len(diff) > limit:
        print("\n... %d more line(s); --limit to show more, --out to save the "
              "cleaned local page" % (len(diff) - limit))

    print("\nExpected sources of difference:")
    print("  - local layout edits not yet published (see `t4 preview`)")
    print("  - content changed in T4 since `t4 mirror` was last run")
    return 1
