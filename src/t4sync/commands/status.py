"""t4 status -- what is pending, grouped by the kind of asset.

Everything T4 stores is pulled into t4-source and deployed with `t4 push`:
page layouts, content layouts, content types, navigation, lists, and the Media
Library items that hold the site's CSS and JS. Grouping the report by kind
matters because the easy mistake is pushing a layout and forgetting the
stylesheet it depends on.

`overrides/` is listed separately and deliberately last: it shadows mirrored
files for local experiments and is never deployed.
"""
import collections
import urllib.error

from ..api import Client
from ..compare import compare, load_manifest
from ..errors import T4Error
from .edit import overrides_root

# Printed in this order; anything unrecognised follows, sorted.
KIND_ORDER = ["pageLayout", "layout", "media", "contenttype", "navigation",
              "list", "channel"]
KIND_LABEL = {
    "pageLayout": "Page layouts",
    "layout": "Content layouts",
    "media": "Media (CSS, JS, snippets)",
    "contenttype": "Content types",
    "navigation": "Navigation objects",
    "list": "Lists",
    "channel": "Channels",
}


def group(results):
    groups = collections.defaultdict(list)
    for entry in results:
        groups[entry["meta"].get("endpoint") or "other"].append(entry)
    return groups


def run(args, project):
    env_name = args.env or project.default_env()
    env = project.env(env_name)
    print("Project     %s" % project.root)
    print("Environment %s" % env["label"])
    print("Push        %s\n" % ("allowed" if env.get("push_allowed") else "disabled"))

    try:
        manifest = load_manifest(project, env_name)
    except T4Error:
        print("Nothing pulled yet - run: t4 pull --env %s" % env_name)
        return 0

    if args.offline:
        counts = collections.Counter(m.get("endpoint") for m in manifest.values())
        print("%d file(s) tracked:" % len(manifest))
        for kind in KIND_ORDER + sorted(set(counts) - set(KIND_ORDER)):
            if counts.get(kind):
                print("  %-26s %d" % (KIND_LABEL.get(kind, kind), counts[kind]))
        print("\nRun without --offline to compare against T4.")
        return 0

    try:
        client = Client(env, project.require_token(env_name), timeout=args.timeout)
        results = compare(project, env_name, client, manifest, args.paths or None,
                          progress_label="  comparing")
    except urllib.error.HTTPError as exc:
        raise T4Error("could not compare: HTTP %s" % exc.code)
    except Exception as exc:
        raise T4Error("could not compare: %s\n"
                      "A T4 instance is usually behind a VPN - check it is "
                      "connected." % type(exc).__name__)

    groups = group(results)
    total_changed = 0

    for kind in KIND_ORDER + sorted(set(groups) - set(KIND_ORDER)):
        entries = groups.get(kind)
        if not entries:
            continue
        changed = [e for e in entries if e["state"] == "changed"]
        missing = [e for e in entries if e["state"] == "local-missing"]
        errors = [e for e in entries if e["state"] == "error"]
        total_changed += len(changed)

        if not (changed or missing or errors):
            if args.all:
                print("%-26s %d file(s), all identical"
                      % (KIND_LABEL.get(kind, kind), len(entries)))
            continue

        print("%s" % KIND_LABEL.get(kind, kind))
        for entry in changed:
            delta = len(entry["local"] or "") - len(entry["remote"] or "")
            print("  M %-52s %+d bytes" % (entry["path"], delta))
        for entry in missing:
            print("  ? %-52s in T4, not on disk" % entry["path"])
        for entry in errors:
            print("  ! %-52s %s" % (entry["path"], entry.get("error", "")))
        print()

    if not total_changed:
        print("Nothing pending - %d file(s) match T4." % len(results))

    overrides = overrides_root(project)
    shadowed = sorted(p for p in overrides.rglob("*") if p.is_file()) \
        if overrides.is_dir() else []
    if shadowed:
        print("Local-only overrides (never deployed)")
        for path in shadowed:
            print("  - %s" % path.relative_to(overrides))
        print("  Edit under t4-source/ instead to deploy a change.\n")

    if total_changed:
        print("Next:  t4 diff --show        review")
        if env.get("push_allowed"):
            print("       t4 push              deploy, one confirmation each")
        else:
            print("       push is disabled for %s - set \"push_allowed\": true"
                  % env_name)
            print("       in .t4/config.json to enable it")
    return 0
