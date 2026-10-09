"""t4 push -- send reviewed local changes back to T4, one confirmation each."""
import datetime
import json
import sys

from ..api import Client
from ..compare import compare, load_manifest, remote_path, unified, write_field
from ..errors import T4Error


def backup(project, env_name, entry, stamp):
    """Snapshot the untouched remote record before modifying it."""
    meta = entry["meta"]
    out = project.env_dir(env_name) / "_backup" / stamp / meta["endpoint"]
    out.mkdir(parents=True, exist_ok=True)
    path = out / ("%s.json" % meta["id"])
    if not path.exists():
        path.write_text(json.dumps(entry["record"], indent=2, sort_keys=True),
                        encoding="utf-8")
    return path


def run(args, project):
    env_name = args.env or project.default_env()
    env = project.env(env_name)

    if not env.get("push_allowed", False):
        raise T4Error(
            "Push is disabled for %r.\n  %s\n"
            "Set \"push_allowed\": true for this environment in .t4/config.json\n"
            "to change that. It is off by default so that a production instance\n"
            "cannot be written to by accident." % (env_name, env["label"]))

    client = Client(env, project.require_token(env_name), timeout=args.timeout)
    manifest = load_manifest(project, env_name)

    print("Target: %s%s\n" % (env["label"], "   [DRY RUN]" if args.dry_run else ""))
    results = compare(project, env_name, client, manifest, args.paths or None,
                      progress_label="Checking against T4")
    changed = [e for e in results if e["state"] == "changed"]

    for entry in [e for e in results if e["state"] == "error"]:
        print("  ! %s  %s" % (entry["path"], entry.get("error", "")))

    if not changed:
        print("Nothing to push - %d file(s) checked, all identical."
              % len([e for e in results if e["state"] == "same"]))
        return 0

    print("%d file(s) differ.\n" % len(changed))
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    pushed = skipped = failed = 0

    for index, entry in enumerate(changed, 1):
        meta = entry["meta"]
        print("=" * 72)
        print("[%d/%d] %s" % (index, len(changed), entry["path"]))
        print("       %s id=%s field=%s  (%s)"
              % (meta["endpoint"], meta["id"], meta["field"], meta.get("name")))
        print("=" * 72)
        for line in unified(entry, from_label="T4 (current)", to_label="local (to push)"):
            sys.stdout.write(line if line.endswith("\n") else line + "\n")

        if args.dry_run:
            print("\n[dry run] would push\n")
            skipped += 1
            continue

        if args.yes:
            answer = "y"
        else:
            try:
                answer = input("\nPush this change to %s? [y/N/q] " % env_name).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nStopped.")
                break
        if answer in ("q", "quit"):
            print("Stopped.")
            break
        if answer not in ("y", "yes"):
            print("Skipped.\n")
            skipped += 1
            continue

        saved = backup(project, env_name, entry, stamp)
        record = write_field(dict(entry["record"]), meta["field"], entry["local"])
        try:
            client.put_json(remote_path(meta), record)
            print("Pushed. Backup: %s\n" % saved.relative_to(project.root))
            pushed += 1
        except Exception as exc:
            print("FAILED: %s\n  remote unchanged; backup kept at %s\n"
                  % (exc, saved.relative_to(project.root)))
            failed += 1

    print("\n%d pushed, %d skipped, %d failed." % (pushed, skipped, failed))
    return 1 if failed else 0
