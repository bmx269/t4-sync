"""t4 sync -- refresh from T4, then report where things stand.

Pull plus a drift report. Never writes to T4: it is the safe "bring me up to
date and tell me what moved" command.
"""
from ..compare import load_manifest
from .pull import pull_env

import json


def snapshot(project, env_name):
    path = project.env_dir(env_name) / "_manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def run(args, project):
    names = project.env_names() if args.all else [args.env or project.default_env()]
    status = 0

    for index, name in enumerate(names):
        if index:
            print()
        if args.all and not project.read_token(name):
            print("Skipping %s: no token stored (t4 token set %s)" % (name, name))
            continue

        before = snapshot(project, name)
        status |= pull_env(project, name, timeout=args.timeout)
        after = snapshot(project, name)

        remote_changed, local_edits, added = [], [], []
        for rel, meta in sorted(after.items()):
            prior = before.get(rel)
            if prior is None:
                added.append(rel)
                continue
            # remote_sha is only set when a local edit blocked the write, so it
            # names exactly the files where local and remote have both moved.
            if meta.get("remote_sha") and meta["remote_sha"] != meta.get("sha"):
                local_edits.append(rel)
            elif prior.get("sha") and prior["sha"] != meta.get("sha"):
                remote_changed.append(rel)

        removed = sorted(set(before) - set(after))

        print()
        if not any((remote_changed, local_edits, added, removed)):
            print("Up to date - nothing changed since the last pull.")
            continue

        def section(title, items):
            if not items:
                return
            print("%s (%d):" % (title, len(items)))
            for rel in items[:15]:
                print("    %s" % rel)
            if len(items) > 15:
                print("    ... and %d more" % (len(items) - 15))

        section("  Changed in T4 since last pull", remote_changed)
        section("  New in T4", added)
        section("  No longer in T4", removed)
        section("  Edited locally, not pushed", local_edits)
        if local_edits:
            print("\n  `t4 diff --show` to review, `t4 push` to deploy them.")

    return status
