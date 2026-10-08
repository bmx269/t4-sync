"""t4 diff -- show where local files differ from T4. Read-only."""
import sys

from ..api import Client
from ..compare import compare, load_manifest, unified

STATE_ORDER = ["changed", "local-missing", "error", "same"]
MARK = {"changed": "M", "local-missing": "?", "error": "!", "same": " "}


def report(results, show=False, include_same=False):
    counts = {}
    for entry in sorted(results, key=lambda e: (STATE_ORDER.index(e["state"]), e["path"])):
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
        if entry["state"] == "same" and not include_same:
            continue
        note = ""
        if entry["state"] == "changed":
            note = "  %+d bytes" % (len(entry["local"] or "") - len(entry["remote"] or ""))
        elif entry["state"] == "error":
            note = "  %s" % entry.get("error", "")
        elif entry["state"] == "local-missing":
            note = "  in T4 but not on disk"
        print(" %s %-58s%s" % (MARK[entry["state"]], entry["path"], note))
        if show and entry["state"] == "changed":
            sys.stdout.writelines(unified(entry))
            print()
    return counts


def run(args, project):
    env_name = args.env or project.default_env()
    env = project.env(env_name)
    client = Client(env, project.require_token(env_name), timeout=args.timeout)
    manifest = load_manifest(project, env_name)

    print("Comparing local against %s\n" % env["label"])
    results = compare(project, env_name, client, manifest, args.paths or None)
    if not results:
        print("Nothing matched %s" % (args.paths,))
        return 1

    counts = report(results, show=args.show, include_same=args.all)
    print("\n%s" % ", ".join("%d %s" % (n, s) for s, n in sorted(counts.items())))
    if counts.get("changed") and not args.show:
        print("Use --show for full diffs, or `t4 push` to deploy them.")
    return 0
